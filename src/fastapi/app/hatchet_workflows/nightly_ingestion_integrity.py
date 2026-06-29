"""Phase 5 of the reliability spec — three-tier nightly integrity sweep.

Runs at 02:00 (Pass 1) and 04:00 (Pass 2):

  - **Pass 1 (02:00)** — detect orphans + dispatch recovery across all
    four tiers. Does NOT bump data_version (embeddings dispatched at
    02:00 may not have landed yet).
  - **Pass 2 (04:00)** — re-runs detection. For every workspace where
    Pass 1 dispatched any recovery work, bumps data_version once. By
    04:00 the embed cron has had two 10-min ticks to catch up and the
    per-completion MV refresh should have fired for any new completions.

Tiers:

  1. **Bronze audit** — files in bronze.manifest with no matching
     silver.reports row, claim-locked + re-dispatched via FastAPI's
     existing ingest_pdf trigger. Spec T10.
  2. **Silver audit** — orphan passages (delegates to Phase 3
     orphan_sweep + the existing 10-min embed cron), plus per-workspace
     Qdrant spot-check (50 random + 50 newest embedding_ids verified
     against the vector store).
  3. **Gold audit** — staleness check + REFRESH for every view in the
     mv_refresh registry, plus ANALYZE on hot silver tables.
  4. **Outbox audit** — stuck outbox.pending_propagations rows nudged
     back to 'pending' status; duplicate-source detection logged for
     manual triage.

Every pass writes a single silver.ingest_progress row with
``stage='integrity_sweep'`` + ``status='completed'`` + an error JSONB
carrying the full report — so the run shows up in the IngestionRuns
UI with the same drill-down treatment as a regular file.

Note: spec text calls for Dagster. This codebase runs every other
scheduled job (mv_refresh_silver, cost_burn_watcher, audit_ledger_verify,
embed_pending_passages, etc.) as Hatchet crons, so we follow that
convention for consistency. The actual scheduling primitive doesn't
affect the spec's invariants.
"""
from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import httpx
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID
from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import hatchet
from app.services.mv_refresh import refresh_views_with_advisory_lock

log = logging.getLogger("georag.hatchet.nightly_ingestion_integrity")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
BRONZE_ORPHAN_AGE_MINUTES = 30
BRONZE_MAX_DISPATCH_ATTEMPTS = 3
BRONZE_CLAIM_LOCK_MINUTES = 15

QDRANT_SAMPLE_SIZE = 50
QDRANT_MISS_RATE_THRESHOLD = 0.05  # 5% — spec Phase 5 Tier 3

OUTBOX_STUCK_AGE_MINUTES = 30
OUTBOX_MAX_ATTEMPTS = 5

ANALYZE_TABLES: tuple[str, ...] = (
    "silver.document_passages",
    "silver.reports",
    "silver.collars",
    "silver.assays_v2",
)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _fastapi_internal_url() -> str:
    return os.environ.get("FASTAPI_INTERNAL_URL", "http://fastapi:8000")


def _qdrant_url() -> str:
    host = os.environ.get("QDRANT_HOST", "qdrant")
    port = os.environ.get("QDRANT_PORT", "6333")
    return f"http://{host}:{port}"


def _detect_pass_number() -> int:
    """Pass 2 fires at 04:00, Pass 1 at 02:00. Allow override via env
    for testing."""
    forced = os.environ.get("INTEGRITY_SWEEP_FORCE_PASS")
    if forced in ("1", "2"):
        return int(forced)
    return 2 if datetime.now(timezone.utc).hour == 4 else 1


# ---------------------------------------------------------------------------
# Workflow + input/output
# ---------------------------------------------------------------------------
class NightlyIntegritySweepInput(BaseModel):
    force_pass: Optional[int] = Field(
        default=None,
        description="Override the auto-detected pass number. 1 or 2; "
                    "None = detect from UTC hour.",
    )


class TierReport(BaseModel):
    tier: int
    name: str
    items_examined: int = 0
    items_dispatched: int = 0
    items_skipped: int = 0
    notes: list[str] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)


class NightlyIntegritySweepOutput(BaseModel):
    pass_number: int
    started_at: str
    duration_ms: int
    tiers: list[TierReport]
    workspaces_data_version_bumped: list[str]


nightly_ingestion_integrity = hatchet.workflow(
    name="nightly_ingestion_integrity",
    on_crons=["0 2 * * *", "0 4 * * *"],
    input_validator=NightlyIntegritySweepInput,
)


# ---------------------------------------------------------------------------
# Tier 1 — Bronze orphan recovery (spec T10)
# ---------------------------------------------------------------------------
async def _tier_1_bronze(pool: asyncpg.Pool) -> TierReport:
    report = TierReport(tier=1, name="bronze_audit")

    # Sweep query — matches the spec almost verbatim; predicate also
    # filters out rows where another sweep instance currently holds a
    # claim lock, and rows that have already exhausted dispatch attempts.
    select_sql = f"""
        SELECT b.file_key, b.workspace_id::text AS workspace_id, b.sha256,
               b.dispatch_attempts, b.uploaded_at, b.document_type
        FROM bronze.manifest b
        LEFT JOIN silver.reports r
               ON r.source_file_sha256 = b.sha256
              AND r.workspace_id       = b.workspace_id
        WHERE r.report_id IS NULL
          AND b.uploaded_at  < now() - interval '{BRONZE_ORPHAN_AGE_MINUTES} minutes'
          AND b.cancelled_at IS NULL
          AND (b.locked_until IS NULL OR b.locked_until < now())
          AND b.dispatch_attempts < {BRONZE_MAX_DISPATCH_ATTEMPTS}
    """
    claim_sql = f"""
        UPDATE bronze.manifest
        SET locked_until      = now() + interval '{BRONZE_CLAIM_LOCK_MINUTES} minutes',
            dispatch_attempts = dispatch_attempts + 1,
            last_dispatch_at  = now()
        WHERE file_key = $1
          AND workspace_id = $2::uuid
          AND (locked_until IS NULL OR locked_until < now())
        RETURNING file_key, dispatch_attempts
    """

    async with pool.acquire() as conn:
        orphans = await conn.fetch(select_sql)
        report.items_examined = len(orphans)

        for orphan in orphans:
            file_key = orphan["file_key"]
            workspace_id = orphan["workspace_id"]

            claim = await conn.fetchrow(claim_sql, file_key, workspace_id)
            if claim is None:
                report.items_skipped += 1
                report.notes.append(f"claim_failed: {file_key}")
                continue

            # Extract project_id from the conventional reports/{projectId}/...
            # prefix. Falls back to None — the FastAPI trigger validates.
            parts = file_key.split("/")
            project_id = parts[1] if len(parts) >= 2 and parts[0] == "reports" else None

            if project_id is None:
                report.items_skipped += 1
                report.notes.append(f"no_project_id: {file_key}")
                continue

            try:
                run_id = await _dispatch_ingest_pdf(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    minio_key=file_key,
                )
                if run_id:
                    report.items_dispatched += 1
                else:
                    report.items_skipped += 1
                    report.notes.append(f"dispatch_returned_none: {file_key}")
            except Exception as exc:
                report.items_skipped += 1
                report.notes.append(f"dispatch_threw: {file_key}: {exc}")
                log.warning("tier1.bronze.dispatch_failed key=%s err=%s", file_key, exc)

    return report


async def _dispatch_ingest_pdf(
    *, workspace_id: str, project_id: str, minio_key: str,
) -> Optional[str]:
    """POST to the existing FastAPI /internal/v1/shadow/ingest_pdf/trigger
    endpoint. Uses the same X-Service-Key as the rest of the bridge."""
    import uuid as _uuid

    service_key = os.environ.get("FASTAPI_SERVICE_KEY")
    if not service_key:
        log.warning("tier1.bronze: FASTAPI_SERVICE_KEY missing; cannot dispatch")
        return None

    # ingest_pdf workflow requires a JWT Authorization header in addition
    # to the service key (matches the existing ShadowRouter flow). The
    # service key is acceptable as a self-signed JWT for internal calls;
    # callers from Laravel use FastApiJwtMinter but inside the FastAPI
    # process we can sign HS256 directly.
    import jwt

    jwt_token = jwt.encode(
        {
            "sub": "0",
            "project_id": project_id,
            "roles": ["shadow:trigger"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        service_key,
        algorithm="HS256",
    )

    url = _fastapi_internal_url().rstrip("/") + "/internal/v1/shadow/ingest_pdf/trigger"
    payload = {
        "workspace_id": workspace_id,
        "project_id": project_id,
        "minio_key": minio_key,
        "file_size": 0,            # parser re-reads from S3 anyway
        "vendor_profile_id": None,
        "correlation_token": str(_uuid.uuid4()),
        "actor_id": None,
    }
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "X-Service-Key": service_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code != 202:
        log.warning(
            "tier1.bronze.trigger non-2xx key=%s status=%s body=%s",
            minio_key, r.status_code, r.text[:200],
        )
        return None
    return (r.json() or {}).get("workflow_run_id")


# ---------------------------------------------------------------------------
# Tier 2 — Silver audit (orphan passages + Qdrant spot-check)
# ---------------------------------------------------------------------------
async def _tier_2_silver(pool: asyncpg.Pool) -> TierReport:
    report = TierReport(tier=2, name="silver_audit")

    # Step 1: delegate orphan passage detection to the Phase 3 helper.
    # It creates recovery ingest_progress rows + advisory-locks per doc.
    try:
        from app.services.ingest.orphan_sweep import claim_and_record_recovery

        claimed, skipped_by_lock = await claim_and_record_recovery(pool)
        report.items_examined += len(claimed) + len(skipped_by_lock)
        report.items_dispatched += sum(1 for c in claimed if c.recovery_run_id is not None)
        report.items_skipped += len(skipped_by_lock)
        report.extras["orphan_passages_claimed"] = len(claimed)
        report.extras["orphan_passages_skipped_by_lock"] = len(skipped_by_lock)
    except Exception as exc:
        report.notes.append(f"orphan_sweep_threw: {exc}")
        log.warning("tier2.silver.orphan_sweep failed: %s", exc)

    # Step 2: Qdrant spot-check per workspace.
    miss_rates = await _tier_2_qdrant_spotcheck(pool)
    report.extras["qdrant_miss_rates"] = miss_rates
    for ws_id, miss_rate in miss_rates.items():
        if miss_rate > QDRANT_MISS_RATE_THRESHOLD:
            report.notes.append(
                f"qdrant_miss_rate workspace={ws_id} rate={miss_rate:.2%}"
            )
    # Phase 6 — publish per-workspace miss-rate gauge so Prometheus +
    # alert manager can fire QdrantMissRateHigh between nightly runs.
    try:
        from app.metrics import QDRANT_SPOTCHECK_MISS_RATE
        for ws_id, miss_rate in miss_rates.items():
            QDRANT_SPOTCHECK_MISS_RATE.labels(workspace_id=ws_id).set(miss_rate)
    except Exception:
        pass

    return report


async def _tier_2_qdrant_spotcheck(pool: asyncpg.Pool) -> dict[str, float]:
    """For each workspace with embedded passages, sample 50 random +
    50 newest embedding_ids, GET each from Qdrant, return per-workspace
    miss-rate map."""
    miss_rates: dict[str, float] = {}
    async with pool.acquire() as conn:
        workspaces = await conn.fetch(
            """
            SELECT DISTINCT workspace_id::text AS ws
            FROM silver.document_passages
            WHERE embedding_id IS NOT NULL
            """,
        )
        for ws_row in workspaces:
            ws_id = ws_row["ws"]
            random_rows = await conn.fetch(
                """
                SELECT embedding_id FROM silver.document_passages
                WHERE workspace_id = $1::uuid AND embedding_id IS NOT NULL
                ORDER BY random() LIMIT $2
                """,
                ws_id, QDRANT_SAMPLE_SIZE,
            )
            newest_rows = await conn.fetch(
                """
                SELECT embedding_id FROM silver.document_passages
                WHERE workspace_id = $1::uuid AND embedding_id IS NOT NULL
                ORDER BY created_at DESC LIMIT $2
                """,
                ws_id, QDRANT_SAMPLE_SIZE,
            )
            sample_ids = list({
                r["embedding_id"]
                for r in (list(random_rows) + list(newest_rows))
                if r["embedding_id"]
            })
            if not sample_ids:
                continue
            misses = await _qdrant_count_misses(sample_ids)
            miss_rates[ws_id] = misses / len(sample_ids)
    return miss_rates


async def _qdrant_count_misses(point_ids: list[str]) -> int:
    """HEAD-equivalent — GET each point and count 404s.

    Collection selection: follows the canonical RAG corpus per ADR-0010.
    When ``settings.RETRIEVAL_USE_DOCUMENT_PASSAGES`` is True (the
    default since 2026-05-28), live writes go to ``georag_chunks``;
    otherwise the legacy ``georag_reports`` collection is the source
    of truth. Hardcoding the legacy name (the pre-2026-06-02 behavior)
    made every new passage look like a miss because new points landed
    in chunks while the sweep checked reports — see P1-B + the
    dead-settings sweep in docs/handover/AUDIT_AND_FIX_REPORT.md.
    """
    from app.config import settings  # local import to avoid module cycle

    collection = (
        "georag_chunks"
        if settings.RETRIEVAL_USE_DOCUMENT_PASSAGES
        else "georag_reports"
    )
    misses = 0
    url_base = _qdrant_url() + f"/collections/{collection}/points/"
    async with httpx.AsyncClient(timeout=10.0) as client:
        for pid in point_ids:
            try:
                r = await client.get(url_base + pid)
                if r.status_code == 404:
                    misses += 1
                elif r.status_code >= 500:
                    # Don't penalise a transient Qdrant blip.
                    log.debug("qdrant.spotcheck 5xx pid=%s", pid)
            except Exception as exc:
                log.debug("qdrant.spotcheck threw pid=%s err=%s", pid, exc)
    return misses


# ---------------------------------------------------------------------------
# Tier 3 — Gold audit (MV refresh + ANALYZE)
# ---------------------------------------------------------------------------
async def _tier_3_gold(pool: asyncpg.Pool) -> TierReport:
    report = TierReport(tier=3, name="gold_audit")

    # Step 1+2+3 — delegate to the Phase 2 service. force=False respects
    # the staleness check (don't pay REFRESH cost when nothing changed).
    try:
        results = await refresh_views_with_advisory_lock(
            pool=pool, workspace_id=None,
            triggered_by="nightly_integrity", force=False,
        )
        report.items_examined = len(results)
        report.items_dispatched = sum(1 for r in results if r.status == "completed")
        report.items_skipped = sum(1 for r in results if r.status == "skipped")
        for r in results:
            if r.status == "failed":
                report.notes.append(f"refresh_failed view={r.view_name} err={r.error}")
        report.extras["view_results"] = [
            {"view_name": r.view_name, "status": r.status,
             "duration_ms": r.duration_ms,
             "rows_before": r.rows_before, "rows_after": r.rows_after}
            for r in results
        ]
    except Exception as exc:
        report.notes.append(f"mv_refresh_threw: {exc}")
        log.warning("tier3.gold.mv_refresh failed: %s", exc)

    # Step 4 — ANALYZE hot tables. Cheap; keeps the planner accurate after
    # bulk ingest.
    async with pool.acquire() as conn:
        for table in ANALYZE_TABLES:
            try:
                await conn.execute(f"ANALYZE {table}")
            except Exception as exc:
                report.notes.append(f"analyze_failed: {table}: {exc}")

    return report


# ---------------------------------------------------------------------------
# Tier 4 — Outbox audit
# ---------------------------------------------------------------------------
async def _tier_4_outbox(pool: asyncpg.Pool) -> TierReport:
    report = TierReport(tier=4, name="outbox_audit")

    async with pool.acquire() as conn:
        # Stuck propagations — pending for too long with attempts left.
        stuck = await conn.fetch(
            f"""
            SELECT id::text AS id, target_store, last_attempted_at, enqueued_at
            FROM outbox.pending_propagations
            WHERE status = 'pending'
              AND enqueued_at < now() - interval '{OUTBOX_STUCK_AGE_MINUTES} minutes'
              AND COALESCE((
                  SELECT count(*) FROM outbox.propagation_attempts
                  WHERE propagation_id = outbox.pending_propagations.id
              ), 0) < {OUTBOX_MAX_ATTEMPTS}
            """,
        )
        report.items_examined = len(stuck)
        # Re-enqueue is a no-op write (touch updated_at). The dispatcher
        # workflow picks them up on its next tick. We don't actually
        # change the status — the existing dispatcher will retry.
        if stuck:
            await conn.execute(
                """
                UPDATE outbox.pending_propagations
                SET enqueued_at = now()
                WHERE id = ANY($1::uuid[])
                """,
                [r["id"] for r in stuck],
            )
            report.items_dispatched = len(stuck)

        # Duplicate-source detection. Log only — re-queue would mask a
        # real bug. Format: same (source_schema, source_table, source_id)
        # in pending status more than once.
        dupes = await conn.fetch(
            """
            SELECT source_schema, source_table, source_id, count(*) AS n
            FROM outbox.pending_propagations
            WHERE status IN ('pending', 'in_flight')
            GROUP BY source_schema, source_table, source_id
            HAVING count(*) > 1
            """,
        )
        if dupes:
            report.notes.append(f"duplicate_sources: {len(dupes)} pairs")
            report.extras["duplicate_sources"] = [
                {"schema": r["source_schema"], "table": r["source_table"],
                 "id": r["source_id"], "count": int(r["n"])}
                for r in dupes
            ]

    # NOTE: Reverb listener-lag check skipped — no Reverb publish-failure
    # log exists in this codebase. Phase 6 observability adds the
    # metric; this tier will pick it up when that lands.

    return report


# ---------------------------------------------------------------------------
# Pass 2 — bump data_version for workspaces that saw recovery work
# ---------------------------------------------------------------------------
async def _bump_data_version_for_recovered_workspaces(
    pool: asyncpg.Pool,
) -> list[str]:
    """Workspaces with at least one ingest_progress row triggered by
    today's integrity sweep get one data_version bump each."""
    bumped: list[str] = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT workspace_id::text AS ws
            FROM silver.ingest_progress
            WHERE triggered_by IN ('nightly_integrity_sweep', 'embed_pending_sweep')
              AND started_at > now() - interval '6 hours'
            """,
        )
        for row in rows:
            ws_id = row["ws"]
            try:
                await conn.execute(
                    "UPDATE silver.workspaces "
                    "SET data_version = data_version + 1, updated_at = NOW() "
                    "WHERE workspace_id = $1::uuid",
                    ws_id,
                )
                bumped.append(ws_id)
            except Exception as exc:
                log.warning("pass2.bump_failed ws=%s err=%s", ws_id, exc)
    return bumped


# ---------------------------------------------------------------------------
# Integrity report row writer
# ---------------------------------------------------------------------------
async def _write_integrity_report_row(
    pool: asyncpg.Pool, pass_number: int, output: NightlyIntegritySweepOutput,
) -> None:
    """Single row per pass in silver.ingest_progress for UI visibility.

    Uses stage='integrity_sweep', status='completed', triggered_by=
    'nightly_integrity_sweep'. The error JSONB carries the full per-tier
    report so the IngestionRuns drill-down has something interesting to
    show.
    """
    import json
    import uuid as _uuid

    payload = {
        "pass": pass_number,
        "tier1_bronze_orphans_dispatched": next(
            (t.items_dispatched for t in output.tiers if t.tier == 1), 0,
        ),
        "tier2_passages_reembedded": next(
            (t.extras.get("orphan_passages_claimed", 0)
             for t in output.tiers if t.tier == 2), 0,
        ),
        "tier2_qdrant_miss_rate": next(
            (t.extras.get("qdrant_miss_rates", {})
             for t in output.tiers if t.tier == 2), {},
        ),
        "tier3_views_refreshed": next(
            (t.extras.get("view_results", []) for t in output.tiers if t.tier == 3), [],
        ),
        "tier4_outbox_events_requeued": next(
            (t.items_dispatched for t in output.tiers if t.tier == 4), 0,
        ),
        "workspaces_data_version_bumped": output.workspaces_data_version_bumped,
    }

    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO silver.ingest_progress (
                    run_id, workspace_id, project_id,
                    minio_key, filename,
                    status, current_stage, current_step,
                    step_index, total_steps,
                    triggered_by, started_at, completed_at, updated_at,
                    error_text
                ) VALUES (
                    gen_random_uuid(),
                    $1::uuid,
                    NULL,
                    $2, $3,
                    'completed', 'integrity_sweep', 'completed',
                    5, 5,
                    'nightly_integrity_sweep', now(), now(), now(),
                    $4
                )
                """,
                LEGACY_DEFAULT_TENANT_UUID,
                f"_integrity_sweep/pass_{pass_number}_{_uuid.uuid4()}",
                f"integrity_sweep_pass_{pass_number}",
                json.dumps(payload),
            )
        except Exception as exc:
            # Best-effort logging row — failure here doesn't change the
            # fact that the actual sweep work completed.
            log.warning("integrity_report.write_failed pass=%d err=%s", pass_number, exc)


# ---------------------------------------------------------------------------
# Workflow entrypoint
# ---------------------------------------------------------------------------
@nightly_ingestion_integrity.task(
    execution_timeout="30m",
    schedule_timeout="2h",
    retries=0,
)
async def sweep(
    input: NightlyIntegritySweepInput, ctx: Context,
) -> NightlyIntegritySweepOutput:
    started_at = datetime.now(timezone.utc)
    t_start = time.monotonic()

    pass_number = input.force_pass if input.force_pass in (1, 2) else _detect_pass_number()
    log.info("nightly_ingestion_integrity.start pass=%d", pass_number)

    pool = await ingest_progress.get_pool()

    tier1 = await _tier_1_bronze(pool)
    log.info("tier1.done examined=%d dispatched=%d skipped=%d",
             tier1.items_examined, tier1.items_dispatched, tier1.items_skipped)

    tier2 = await _tier_2_silver(pool)
    log.info("tier2.done examined=%d dispatched=%d",
             tier2.items_examined, tier2.items_dispatched)

    tier3 = await _tier_3_gold(pool)
    log.info("tier3.done examined=%d dispatched=%d",
             tier3.items_examined, tier3.items_dispatched)

    tier4 = await _tier_4_outbox(pool)
    log.info("tier4.done examined=%d dispatched=%d",
             tier4.items_examined, tier4.items_dispatched)

    workspaces_bumped: list[str] = []
    if pass_number == 2:
        workspaces_bumped = await _bump_data_version_for_recovered_workspaces(pool)
        log.info("pass2.data_version_bumped count=%d", len(workspaces_bumped))

    output = NightlyIntegritySweepOutput(
        pass_number=pass_number,
        started_at=started_at.isoformat(),
        duration_ms=int((time.monotonic() - t_start) * 1000),
        tiers=[tier1, tier2, tier3, tier4],
        workspaces_data_version_bumped=workspaces_bumped,
    )

    await _write_integrity_report_row(pool, pass_number, output)

    log.info(
        "nightly_ingestion_integrity.complete pass=%d duration_ms=%d "
        "tiers=[%s]",
        pass_number, output.duration_ms,
        ", ".join(f"T{t.tier}:exam={t.items_examined}/disp={t.items_dispatched}"
                  for t in output.tiers),
    )

    return output


__all__ = [
    "nightly_ingestion_integrity",
    "NightlyIntegritySweepInput",
    "NightlyIntegritySweepOutput",
    "TierReport",
]
