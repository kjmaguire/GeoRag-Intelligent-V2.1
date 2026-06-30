"""Stale-run detector cron — Phase 1 / Fix 1e of the reliability spec.

Every 15 minutes, scan silver.ingest_progress for rows that are
``status='started'`` but whose ``last_heartbeat_at`` is older than 15
minutes. These are the carcasses of:

  - Hatchet worker crashes (SIGKILL, OOM, Docker restart)
  - Hung task subprocess that never raised but never returned
  - Concurrency-cancelled runs that didn't fire on_failure_task

For each stale candidate we apply one of three resolutions:

  1. **Race recovery** — if the run is at ``embed_verify``/``embedding`` and
     the project actually has zero unembedded passages, the embed already
     finished but nothing flipped ``status='completed'`` (the embed
     completion sweep races against the heartbeat clock). Mark completed
     instead of timing out so the UI reflects reality.
  2. **Retry dispatch** — if the run died at ``preflight``/``parse``/
     ``persist`` (the actual file-processing stages) AND we have not
     already retried it ``RECOVERY_MAX_ATTEMPTS`` times, mark this row
     ``timed_out`` and spawn a fresh ``ingest_pdf`` workflow with
     ``triggered_by='stale_run_sweep'`` and ``parent_run_id`` set. This
     gives observable lineage: every retry is an auditable attempt with a
     known parent + reason. ``attempt_number`` is derived server-side
     inside ``start_run`` so the cap is enforced even with concurrent
     sweep instances.
  3. **Mark timed_out** — every other case (out of retries, terminal
     stage that we can't recover, etc.) just marks the row terminal so
     the UI flips and the alert metrics fire.

The conditional update inside ``mark_timed_out`` / ``mark_completed_by_run``
silently no-ops if the row already transitioned (e.g. on_failure_task beat
us to it). This is the durable backstop for Bug 1 (silent stalls).

See [[ingestion-reliability-spec]] for the full state-machine contract.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import hatchet
from app.services.laravel_bridge import post_ingestion_progress

log = logging.getLogger("georag.hatchet.stale_run_detector")


# Default 15 minutes — matches the spec. Configurable via env so we can
# tighten it in tests without redeploying.
def _stale_after_minutes() -> int:
    raw = os.environ.get("STALE_RUN_DETECTOR_MINUTES", "15")
    try:
        v = int(raw)
        return v if v > 0 else 15
    except ValueError:
        return 15


def _recovery_max_attempts() -> int:
    """Cap the parent-chain depth so a chronically broken file can't loop forever.

    Counts the attempt_number of the doomed row; 3 means: original
    upload + 2 sweep-driven retries before we give up and just leave the
    row timed_out for manual investigation.
    """
    raw = os.environ.get("STALE_RUN_RECOVERY_MAX_ATTEMPTS", "3")
    try:
        v = int(raw)
        return v if v > 0 else 3
    except ValueError:
        return 3


# Stages where a stale heartbeat genuinely means the parse work was lost
# and a re-dispatch will produce useful progress. Stages downstream of
# persist already have rows in silver.reports; re-running ingest_pdf for
# them would just duplicate work or hit the dedupe-on-sha256 path. For
# embed_verify/embedding the embed_pending_passages cron is the recovery
# path, not ingest_pdf.
RETRY_STAGES: frozenset[str] = frozenset({"preflight", "parse", "persist"})


class StaleRunDetectorInput(BaseModel):
    stale_minutes: int = Field(
        default=15, ge=1, le=240,
        description="Mark a 'started' run timed_out if last_heartbeat is "
                    "older than this many minutes.",
    )


class StaleRunDetectorOutput(BaseModel):
    runs_scanned: int
    runs_marked_completed: int = 0
    runs_marked_timed_out: int
    recovery_runs_dispatched: int = 0
    broadcasts_emitted: int
    sampled_at: datetime


stale_run_detector = hatchet.workflow(
    name="stale_run_detector",
    on_crons=["*/15 * * * *"],
    input_validator=StaleRunDetectorInput,
)


async def _project_is_fully_embedded(pool, project_id: str | None) -> bool:
    """True when every passage on this project has an embedding_id.

    Returns False on any DB error so we err on the side of timing out
    (the safe, observable outcome) rather than silently marking
    completed.
    """
    if not project_id:
        return False
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM silver.document_passages p
                    JOIN silver.reports r ON r.report_id = p.document_id
                    WHERE r.project_id = $1::uuid
                      AND p.embedding_id IS NULL
                ) AS has_unembedded
                """,
                project_id,
            )
        return not bool(row and row["has_unembedded"])
    except Exception as exc:
        log.warning(
            "stale_run_detector: unembedded-check failed for project=%s: %s",
            project_id, exc,
        )
        return False


async def _dispatch_recovery_run(
    *,
    stale_row: dict,
) -> str | None:
    """Spawn a fresh ingest_pdf workflow tied to the stale row by parent_run_id.

    Returns the new run_id on success, None on any failure (caller will
    fall back to plain timed_out without recovery).
    """
    try:
        # Imported lazily — keeps the stale_run_detector worker bootable
        # even when ingest_pdf has an import-time error.
        from uuid import uuid4

        from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf

        # file_size is informational only — preflight re-downloads and
        # re-derives size against the 2 GB cap from the actual bytes, so
        # passing 0 here is safe. (Searched: input.file_size has zero
        # references in ingest_pdf.py outside the input model.)
        # Reserve the per-run row BEFORE dispatching, so on_failure_task
        # (if dispatch fails immediately) can still find a row to update.
        recovery_run_id = await ingest_progress.start_run(
            workspace_id=stale_row["workspace_id"],
            project_id=stale_row["project_id"],
            minio_key=stale_row["minio_key"],
            triggered_by="stale_run_sweep",
            parent_run_id=stale_row["run_id"],
            recovery_reason="stale_heartbeat",
        )
        if recovery_run_id is None:
            log.warning(
                "stale_run_detector: start_run returned None — skipping "
                "recovery dispatch for run=%s", stale_row["run_id"],
            )
            return None

        payload = IngestPdfInput(
            workspace_id=stale_row["workspace_id"],
            project_id=stale_row["project_id"],
            minio_key=stale_row["minio_key"],
            file_size=0,
            correlation_token=f"stale-sweep-{uuid4()}",
        )
        ref = await ingest_pdf.aio_run_no_wait(payload)
        log.info(
            "stale_run_detector: dispatched recovery ingest_pdf "
            "parent=%s recovery=%s workflow_run_id=%s key=%s",
            stale_row["run_id"], recovery_run_id, ref.workflow_run_id,
            stale_row["minio_key"],
        )
        return recovery_run_id
    except Exception as exc:
        log.warning(
            "stale_run_detector: recovery dispatch failed for run=%s key=%s: %s",
            stale_row["run_id"], stale_row.get("minio_key"), exc,
        )
        return None


@stale_run_detector.task(execution_timeout="2m", schedule_timeout="1h", retries=1)
async def detect(input: StaleRunDetectorInput, ctx: Context) -> StaleRunDetectorOutput:
    stale_minutes = input.stale_minutes or _stale_after_minutes()
    max_attempts = _recovery_max_attempts()

    pool = await ingest_progress.get_pool()
    runs_marked_completed = 0
    runs_marked_timed_out = 0
    recovery_runs_dispatched = 0
    broadcasts_emitted = 0

    # Select runs to sweep. Done outside the per-row UPDATE so we can
    # log + broadcast each one individually without holding a long lock.
    # current_step is read (not current_stage) because it's the field
    # written by mark_stage_started — current_stage is set lazily by the
    # task body and is often NULL on rows that died early in a stage.
    select_sql = f"""
        SELECT run_id::text          AS run_id,
               workspace_id::text    AS workspace_id,
               project_id::text      AS project_id,
               minio_key,
               filename,
               current_stage,
               current_step,
               attempt_number,
               triggered_by
          FROM silver.ingest_progress
         WHERE status = 'started'
           AND last_heartbeat_at < now() - interval '{int(stale_minutes)} minutes'
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(select_sql)

    log.info("stale_run_detector: %d candidate run(s) older than %dm", len(rows), stale_minutes)

    # Phase 6 — publish the active 'started' count gauge so the
    # IngestionRunStuckStarted alert can fire even when no transitions
    # happen on this tick.
    try:
        async with pool.acquire() as gauge_conn:
            active_row = await gauge_conn.fetchrow(
                "SELECT count(*)::int AS n FROM silver.ingest_progress WHERE status = 'started'"
            )
        from app.metrics import INGESTION_STALE_RUNS_DETECTED
        INGESTION_STALE_RUNS_DETECTED.set(int(active_row["n"]) if active_row else 0)
    except Exception:
        pass

    for row in rows:
        run_id = row["run_id"]
        current_step = row["current_step"] or "unknown"

        # Resolution 1 — race recovery. The embed completion sweep didn't
        # win the race against this 15-min tick, but the embeddings are
        # actually all in. Mark completed instead of timing out.
        if current_step in {"embed_verify", "embedding"} and \
                await _project_is_fully_embedded(pool, row["project_id"]):
            transitioned = await ingest_progress.mark_completed_by_run(run_id=run_id)
            if transitioned:
                runs_marked_completed += 1
                if row["project_id"]:
                    try:
                        await post_ingestion_progress(
                            workspace_id=row["workspace_id"],
                            project_id=row["project_id"],
                            run_id=run_id,
                            stage="embedding",
                            status="completed",
                            message="Recovered by stale sweep — embeddings already complete.",
                        )
                        broadcasts_emitted += 1
                    except Exception as exc:
                        log.warning("stale_run_detector: race-recovery broadcast failed run=%s: %s", run_id, exc)
                log.info(
                    "stale_run_detector: race-recovered run=%s (project already fully embedded)",
                    run_id,
                )
                continue
            # Lost the race to another writer — fall through to the
            # normal terminal path below.

        # Resolution 2 — retry-eligible. Mark the doomed row timed_out
        # AND spawn a fresh ingest_pdf run linked by parent_run_id.
        will_retry = (
            current_step in RETRY_STAGES
            and (row["attempt_number"] or 1) < max_attempts
            and row["minio_key"]
            and row["workspace_id"]
            and row["project_id"]
        )

        # Resolution 3 (default) — mark timed_out. Always happens for
        # rows we decline to recover/retry, AND happens BEFORE the
        # recovery dispatch so the original row reaches its terminal
        # state regardless of dispatch success.
        transitioned = await ingest_progress.mark_timed_out(
            run_id=run_id, reason="stale_heartbeat",
        )
        if not transitioned:
            # Lost the race to on_failure_task or another sweep instance —
            # don't dispatch a recovery on a row someone else closed.
            continue
        runs_marked_timed_out += 1

        try:
            await post_ingestion_progress(
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                run_id=run_id,
                stage=row["current_stage"] or current_step,
                status="timed_out",
                message=f"No heartbeat for {stale_minutes}m; marked timed_out by sweep.",
            )
            broadcasts_emitted += 1
        except Exception as e:
            log.warning("stale_run_detector: broadcast failed run=%s: %s", run_id, e)

        if will_retry:
            recovery_run_id = await _dispatch_recovery_run(stale_row=dict(row))
            if recovery_run_id is not None:
                recovery_runs_dispatched += 1

    return StaleRunDetectorOutput(
        runs_scanned=len(rows),
        runs_marked_completed=runs_marked_completed,
        runs_marked_timed_out=runs_marked_timed_out,
        recovery_runs_dispatched=recovery_runs_dispatched,
        broadcasts_emitted=broadcasts_emitted,
        sampled_at=datetime.utcnow(),
    )
