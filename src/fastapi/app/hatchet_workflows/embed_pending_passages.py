"""embed_pending_passages Hatchet workflow (§04i Layer 5 enablement).

Doc-phase 183 — Phase E.1 Track 3.

Wraps `app.services.ingest.passage_embedder.embed_pending_passages` as
a Hatchet workflow so passage embeddings land in Qdrant on a schedule
or post-cluster-ingest trigger.

Manual invocation:
  embed_pending_passages_wf.run({"workspace_id": "<uuid>", "project_id": "<uuid>"})

Cron-fire (when project_id="*"): walks all projects with un-embedded
passages and syncs them. Cron schedule omitted for now — operator
triggers manually after each cluster ingest.
"""
from __future__ import annotations

import logging
import os

import asyncpg
from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
)
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.ingest.passage_embedder import embed_pending_passages

log = logging.getLogger("georag.hatchet.embed_pending_passages")


class EmbedPendingPassagesInput(BaseModel):
    workspace_id: str = Field(
        default="a0000000-0000-0000-0000-000000000001",
        description="Workspace UUID for RLS scoping. Default = Default Workspace.",
    )
    project_id: str = Field(
        default="*",
        description="Project UUID to embed, or '*' to walk every project with "
                    "un-embedded passages.",
    )
    batch_size: int = Field(default=32)
    max_passages: int | None = Field(
        default=None,
        description="Cap for smoke runs. None = no limit.",
    )


class EmbedPendingPassagesOutput(BaseModel):
    projects_processed: int
    total_seen: int = 0
    total_embedded: int = 0
    total_upserted: int = 0
    total_skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    # Phase 3 reliability spec — count of recovery ingest_progress rows
    # created on this sweep. Exposed for nightly integrity reports.
    recovery_runs_created: int = 0


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


embed_pending_passages_wf = hatchet.workflow(
    name="embed_pending_passages",
    # Doc-phase 183 — daily embed sync at 05:45 UTC (after kg_sync at
    # 05:30 and before the next day's eval_real_rag_nightly).
    # 2026-05-22 — added an "every 10 minutes" safety-net cron so that
    # when the persist-side inline trigger races with a Hatchet retry
    # (BattleNorth bug), unembedded passages get picked up within ~10 min
    # instead of waiting a full day. The function is idempotent (passages
    # already with embedding_id get skipped) so frequent runs are cheap
    # when nothing is pending.
    on_crons=["45 5 * * *", "*/10 * * * *"],
    input_validator=EmbedPendingPassagesInput,
    # Per-workspace singleton. The every-10-min safety-net cron + daily
    # cron + manual triggers all queue behind the in-flight run for the
    # same workspace; different workspaces still embed in parallel.
    # GROUP_ROUND_ROBIN queues rather than cancels so an in-flight large
    # bulk run can't be interrupted by a tiny safety-net tick.
    concurrency=ConcurrencyExpression(
        expression="input.workspace_id",
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


@embed_pending_passages_wf.task(execution_timeout="2h", schedule_timeout="2h", retries=0)
async def run(
    input: EmbedPendingPassagesInput, ctx: Context
) -> EmbedPendingPassagesOutput:
    if input.project_id == "*":
        conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
        try:
            rows = await conn.fetch(
                "SELECT DISTINCT r.project_id::text AS pid "
                "  FROM silver.document_passages dp "
                "  JOIN silver.reports r ON r.report_id = dp.document_id "
                " WHERE dp.embedding_id IS NULL AND r.project_id IS NOT NULL"
            )
            project_ids = [r["pid"] for r in rows]
        finally:
            await conn.close()
    else:
        project_ids = [input.project_id]

    # Phase 3 of the reliability spec — orphan-document recovery layer.
    # Before the per-project embed loop runs, walk silver.document_passages
    # for any document with un-embedded passages older than 5 minutes,
    # take a per-document advisory lock, and create a recovery
    # ingest_progress row linked back to the original via parent_run_id.
    #
    # This gives us observable lineage: every safety-net dispatch is now
    # an auditable attempt with a known parent + reason, not a silent
    # background catch-up.
    try:
        from app.hatchet_workflows import _progress as _ingest_progress
        from app.services.ingest.orphan_sweep import claim_and_record_recovery

        sweep_pool = await _ingest_progress.get_pool()
        claimed, skipped_by_lock = await claim_and_record_recovery(sweep_pool)
        recovery_runs_created = sum(1 for c in claimed if c.recovery_run_id is not None)
        log.info(
            "embed_pending_passages.orphan_sweep claimed=%d (recovery_runs=%d) "
            "skipped_by_lock=%d",
            len(claimed), recovery_runs_created, len(skipped_by_lock),
        )
    except Exception as exc:
        # Recovery-run creation is observability — a failure here must
        # never block the actual embed work below.
        log.warning("embed_pending_passages.orphan_sweep failed: %s", exc)
        recovery_runs_created = 0

    # Phase 6 — publish per-workspace embed-pending gauge. The
    # EmbedPendingPassagesStuck alert fires when any workspace has a
    # non-zero value for 20+ minutes, which catches a stalled embed
    # worker that the orphan sweep failed to recover from.
    try:
        gauge_conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
        try:
            gauge_rows = await gauge_conn.fetch(
                """
                SELECT r.workspace_id::text AS ws, count(*)::int AS n
                FROM silver.document_passages dp
                JOIN silver.reports r ON r.report_id = dp.document_id
                WHERE dp.embedding_id IS NULL
                GROUP BY r.workspace_id
                """,
            )
        finally:
            await gauge_conn.close()
        from app.metrics import EMBED_PENDING_PASSAGES
        for gr in gauge_rows:
            EMBED_PENDING_PASSAGES.labels(workspace_id=gr["ws"]).set(int(gr["n"]))
    except Exception as exc:
        log.debug("embed_pending_passages: gauge publish failed: %s", exc)

    log.info("embed_pending_passages.start projects=%d", len(project_ids))

    total_seen = 0
    total_embedded = 0
    total_upserted = 0
    total_skipped = 0
    errors: list[str] = []

    for pid in project_ids:
        try:
            r = await embed_pending_passages(
                workspace_id=input.workspace_id,
                project_id=pid,
                batch_size=input.batch_size,
                max_passages=input.max_passages,
            )
            total_seen += r.passages_seen
            total_embedded += r.passages_embedded
            total_upserted += r.qdrant_points_upserted
            total_skipped += r.passages_skipped
            errors.extend(r.errors)
        except Exception as e:
            errors.append(f"project={pid}:{type(e).__name__}:{e}")
            log.warning(
                "embed_pending_passages.project_failed pid=%s err=%s", pid, e,
            )

    # Orphan / cross-project pass: passages without a parent report
    # (chunk_kind in {'public_geo_synthesis','kg_narrative',
    # 'structured_summary',...}) have document_id NULL so the per-project
    # loop above never touches them. Run an unscoped sweep so the TIER 0b
    # public-geo backfill and ADR-0012 synthesizer outputs get embedded.
    if input.project_id == "*":
        try:
            r = await embed_pending_passages(
                workspace_id=input.workspace_id,
                project_id=None,
                batch_size=input.batch_size,
                max_passages=input.max_passages,
            )
            total_seen += r.passages_seen
            total_embedded += r.passages_embedded
            total_upserted += r.qdrant_points_upserted
            total_skipped += r.passages_skipped
            errors.extend(r.errors)
            log.info(
                "embed_pending_passages.orphan_pass seen=%d embedded=%d upserted=%d",
                r.passages_seen, r.passages_embedded, r.qdrant_points_upserted,
            )
        except Exception as e:
            errors.append(f"orphan_pass:{type(e).__name__}:{e}")
            log.warning("embed_pending_passages.orphan_pass_failed err=%s", e)

    log.info(
        "embed_pending_passages.complete projects=%d seen=%d embedded=%d "
        "upserted=%d skipped=%d errors=%d",
        len(project_ids), total_seen, total_embedded, total_upserted,
        total_skipped, len(errors),
    )

    # Sweep silver.ingest_progress: any row sitting at embed_verify/embedding
    # for a project that now has zero unembedded passages is logically
    # finished — flip it to 'completed' so the UI bar fills.
    #
    # Two-step (per-run) instead of one big UPDATE so we can:
    #   1. Use the canonical mark_completed_by_run (conditional terminal
    #      update + Prometheus metrics + status='completed' enum write).
    #      The previous one-shot UPDATE only set current_step='completed'
    #      and left status='started', which then got clobbered to
    #      'timed_out' by stale_run_detector 15 minutes later.
    #   2. Emit the per-run Reverb broadcast so IngestionRuns.tsx flips
    #      to "Completed" without waiting for its poll tick.
    try:
        from app.services.laravel_bridge import post_ingestion_progress
        sweep_pool2 = await _ingest_progress.get_pool()
        async with sweep_pool2.acquire() as sweep_conn:
            rows_to_complete = await sweep_conn.fetch(
                """
                SELECT ip.run_id::text       AS run_id,
                       ip.workspace_id::text AS workspace_id,
                       ip.project_id::text   AS project_id
                FROM silver.ingest_progress ip
                WHERE ip.status NOT IN ('completed','failed','cancelled','timed_out')
                  AND ip.current_step IN ('embed_verify', 'embedding')
                  AND ip.project_id::text = ANY($1::text[])
                  AND NOT EXISTS (
                        SELECT 1
                        FROM silver.document_passages p
                        JOIN silver.reports r ON r.report_id = p.document_id
                        WHERE r.project_id = ip.project_id
                          AND p.embedding_id IS NULL
                  )
                """,
                project_ids,
            )

        flipped = 0
        for r in rows_to_complete:
            transitioned = await _ingest_progress.mark_completed_by_run(
                run_id=r["run_id"],
            )
            if not transitioned:
                continue
            flipped += 1
            try:
                await post_ingestion_progress(
                    workspace_id=r["workspace_id"],
                    project_id=r["project_id"],
                    run_id=r["run_id"],
                    stage="embedding",
                    status="completed",
                    message="Ingestion complete; all chunks embedded.",
                )
            except Exception as exc:
                log.warning(
                    "embed_pending_passages.completion_broadcast failed "
                    "run=%s err=%s", r["run_id"], exc,
                )
        if flipped:
            log.info(
                "embed_pending_passages: marked %d ingest_progress run(s) "
                "completed via sweep", flipped,
            )
    except Exception as e:
        log.warning("embed_pending_passages: ingest_progress sweep failed: %s", e)

    return EmbedPendingPassagesOutput(
        projects_processed=len(project_ids),
        total_seen=total_seen,
        total_embedded=total_embedded,
        total_upserted=total_upserted,
        total_skipped=total_skipped,
        errors=errors,
        recovery_runs_created=recovery_runs_created,
    )


__all__ = [
    "embed_pending_passages_wf",
    "EmbedPendingPassagesInput",
    "EmbedPendingPassagesOutput",
]
