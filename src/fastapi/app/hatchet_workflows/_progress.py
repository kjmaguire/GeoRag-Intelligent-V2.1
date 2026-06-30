"""Per-file ingestion progress writer (Phase 1 — per-run rows).

Tiny module called by each step of the ingest_pdf + tiff_normalize Hatchet
workflows so the IngestionRunsController can show real per-file progress
bars instead of the time-elapsed heuristics Phase A shipped with.

Writes into silver.ingest_progress (the Phase B table extended by the
2026_05_25 reliability migration). Originally one row per (workspace_id,
minio_key); now one row per run with parent_run_id linking recovery work
to the original. All terminal-state writes use the conditional-update
pattern so a delayed worker can't overwrite a previously-set terminal
state.

Step model (5 logical steps):
    1  preflight
    2  parse
    3  persist
    4  embed_verify
    5  embedding         (set by the embed dispatcher when all chunks have ids)

State machine:
    queued  → started
    started → completed | failed | timed_out
    queued  → cancelled

Terminal states: completed, failed, cancelled, timed_out (immutable).

Best-effort error handling: a DB failure inside a progress helper must
never block the surrounding workflow task, so every helper swallows
exceptions and just logs. Hatchet's on_failure_task is the durable
backstop — if a progress write goes missing, the workflow-level hook
re-asserts the terminal state.

See [[ingestion-runs-ui-2026-05-24]] for design notes and
[[ingestion-reliability-spec]] for the per-run schema rationale.
"""

from __future__ import annotations

import logging
import os
import uuid

import asyncpg

log = logging.getLogger("georag.hatchet.progress")

# Ordered list — index in this list = step_index written to the DB.
STEPS: tuple[str, ...] = (
    "preflight",
    "parse",
    "persist",
    "embed_verify",
    "embedding",
)
TOTAL_STEPS = len(STEPS)

TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled", "timed_out")
ALLOWED_TRIGGERS: tuple[str, ...] = (
    "upload",
    "embed_pending_sweep",
    "nightly_integrity_sweep",
    "manual_retry",
    "stale_run_sweep",
)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


# ---------------------------------------------------------------------------
# Module-level asyncpg pool — spec constraint #3: hooks/sweeps reuse the pool.
# ---------------------------------------------------------------------------
_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the lazily-initialised module pool.

    The Hatchet worker is a single-process asyncio runtime, so one
    module-level pool is safe and avoids the per-call connect overhead the
    Phase B implementation paid for every write.
    """
    global _pool
    if _pool is None or _pool.is_closing():
        _pool = await asyncpg.create_pool(
            _dsn(),
            min_size=1,
            max_size=4,
            statement_cache_size=0,
        )
    return _pool


def _filename_from_key(minio_key: str) -> str:
    return minio_key.rsplit("/", 1)[-1] if "/" in minio_key else minio_key


def _step_index(stage: str) -> int:
    """Return 1-indexed step number, or 0 for non-step stages (e.g. 'queued')."""
    try:
        return STEPS.index(stage) + 1
    except ValueError:
        return 0


def _record_terminal_metrics(
    *, status: str, triggered_by: str, duration_seconds: float,
) -> None:
    """Best-effort Prometheus instrumentation for terminal-state writes.

    Catches and swallows any error so a metrics-system outage cannot
    block the durable DB write — the invariant is the row, not the
    histogram bucket.
    """
    try:
        from app.metrics import INGESTION_RUN_DURATION, INGESTION_RUNS_TOTAL
        INGESTION_RUNS_TOTAL.labels(
            status=status, triggered_by=triggered_by,
        ).inc()
        INGESTION_RUN_DURATION.labels(
            status=status, triggered_by=triggered_by,
        ).observe(max(0.0, duration_seconds))
    except Exception:
        # Metrics are an observability layer; never fail the data write.
        pass


# ---------------------------------------------------------------------------
# Per-run API (the new Phase 1 surface)
# ---------------------------------------------------------------------------
async def start_run(
    *,
    workspace_id: str,
    project_id: str,
    minio_key: str,
    triggered_by: str = "upload",
    parent_run_id: str | None = None,
    recovery_reason: str | None = None,
    workflow_run_id: str | None = None,
) -> str | None:
    """Insert a fresh ingest_progress row and return the new run_id.

    Idempotency: this always INSERTs. Recovery dispatches (sweep,
    nightly_integrity, manual_retry) create new rows linked to the
    original via parent_run_id — they never mutate a previously-terminal
    row. ``attempt_number`` is derived server-side from prior attempts on
    the same (workspace_id, minio_key) so the UI's "attempt 3 of N" badge
    stays accurate.

    Returns None on DB failure (best-effort).
    """
    if triggered_by not in ALLOWED_TRIGGERS:
        log.warning("progress.start_run: unknown triggered_by=%r (forcing 'upload')", triggered_by)
        triggered_by = "upload"

    filename = _filename_from_key(minio_key)
    new_run_id = str(uuid.uuid4())

    sql = """
        INSERT INTO silver.ingest_progress (
            run_id, workspace_id, project_id, workflow_run_id,
            minio_key, filename,
            status, current_stage, current_step,
            step_index, total_steps,
            triggered_by, parent_run_id, recovery_reason,
            attempt_number,
            started_at, updated_at
        )
        SELECT
            $1::uuid, $2::uuid, $3::uuid, $4,
            $5, $6,
            'queued', NULL, 'queued',
            0, $7,
            $8, $9::uuid, $10,
            COALESCE((
                SELECT MAX(attempt_number) + 1
                FROM silver.ingest_progress
                WHERE workspace_id = $2::uuid AND minio_key = $5
            ), 1),
            now(), now()
        RETURNING run_id::text
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                new_run_id,
                workspace_id,
                project_id,
                workflow_run_id,
                minio_key,
                filename,
                TOTAL_STEPS,
                triggered_by,
                parent_run_id,
                recovery_reason,
            )
        return row["run_id"] if row else new_run_id
    except Exception as e:
        log.warning("progress.start_run failed (key=%s): %s", minio_key, e)
        return None


async def mark_stage_started(
    *,
    run_id: str,
    stage: str,
    worker_id: str | None = None,
) -> None:
    """Mark the given stage as in-progress for this run.

    Uses the conditional-update pattern (Fix 1a): the update is a no-op
    if ``status`` is already terminal. This prevents a delayed step from
    re-opening a workflow the failure hook already closed.

    Also flips status from 'queued' → 'started' on the first stage transition.
    """
    if stage not in STEPS and stage != "queued":
        log.warning("progress.mark_stage_started: unknown stage %r", stage)
        return

    sql = """
        UPDATE silver.ingest_progress
        SET current_stage         = $2,
            current_step          = $2,
            step_index            = $3,
            last_stage_started_at = now(),
            last_heartbeat_at     = now(),
            step_started_at       = now(),
            worker_id             = COALESCE($4, worker_id),
            status                = CASE WHEN status = 'queued' THEN 'started' ELSE status END,
            updated_at            = now()
        WHERE run_id = $1::uuid
          AND status NOT IN ('completed','failed','cancelled','timed_out')
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql, run_id, stage, _step_index(stage), worker_id)
    except Exception as e:
        log.warning("progress.mark_stage_started failed (run=%s stage=%s): %s", run_id, stage, e)


async def mark_heartbeat(*, run_id: str) -> None:
    """Bump last_heartbeat_at for a running task.

    The 15-min stale_run_detector cron uses this to detect dead workers.
    No-op if the run is no longer in 'started' state.
    """
    sql = """
        UPDATE silver.ingest_progress
        SET last_heartbeat_at = now(),
            updated_at        = now()
        WHERE run_id = $1::uuid AND status = 'started'
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql, run_id)
    except Exception as e:
        log.warning("progress.mark_heartbeat failed (run=%s): %s", run_id, e)


import asyncio
import contextlib


@contextlib.asynccontextmanager
async def heartbeat_loop(
    *,
    workspace_id: str,
    minio_key: str,
    interval_seconds: float = 30.0,
):
    """Async context manager that runs a background heartbeat ticker.

    Resolves the active run_id from (workspace_id, minio_key) once at
    entry, then bumps last_heartbeat_at every ``interval_seconds`` until
    the with-block exits. Used in long-running ingest_pdf tasks (parse,
    persist, p04p_dual_write) so the stale_run_detector cron knows the
    worker is still alive.

    Usage::

        async with ingest_progress.heartbeat_loop(
            workspace_id=ws, minio_key=key,
        ):
            await do_long_work()

    Best-effort: if the run_id can't be resolved, the loop becomes a
    no-op. The surrounding task keeps running.
    """
    run_id = await lookup_active_run_id(workspace_id=workspace_id, minio_key=minio_key)
    task: asyncio.Task | None = None
    if run_id is not None:

        async def _ticker() -> None:
            try:
                while True:
                    await asyncio.sleep(interval_seconds)
                    await mark_heartbeat(run_id=run_id)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(_ticker(), name=f"hb-{run_id[:8]}")
    try:
        yield run_id
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def mark_completed_by_run(
    *,
    run_id: str,
    report_id: str | None = None,
) -> bool:
    """Terminal write — sets status=completed via conditional update.

    Returns True if the row actually transitioned (i.e. wasn't already
    terminal). Callers that gate side effects (mv_refresh, data_version
    bump, workspace.data_updated broadcast) should branch on the return
    value to avoid double-firing on retried hooks.
    """
    sql = """
        UPDATE silver.ingest_progress
        SET status        = 'completed',
            current_step  = 'completed',
            current_stage = 'completed',
            step_index    = total_steps,
            completed_at  = now(),
            updated_at    = now(),
            report_id     = COALESCE($2::uuid, report_id),
            error_text    = NULL,
            failed_at     = NULL
        WHERE run_id = $1::uuid
          AND status NOT IN ('completed','failed','cancelled','timed_out')
        RETURNING run_id, triggered_by,
                  EXTRACT(EPOCH FROM (now() - started_at))::float AS duration_seconds
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, run_id, report_id)
        transitioned = row is not None
        if not transitioned:
            log.info("progress.mark_completed: no-op (already terminal) run=%s", run_id)
            return False
        _record_terminal_metrics(
            status="completed",
            triggered_by=row["triggered_by"] or "upload",
            duration_seconds=float(row["duration_seconds"] or 0.0),
        )
        return True
    except Exception as e:
        log.warning("progress.mark_completed failed (run=%s): %s", run_id, e)
        return False


async def mark_failed_by_run(
    *,
    run_id: str,
    stage: str | None = None,
    error: str,
) -> bool:
    """Terminal write — sets status=failed via conditional update.

    Records current_stage so the IngestionRuns UI can show "failed at
    persist" instead of just "failed". Returns True iff the row actually
    transitioned.
    """
    sql = """
        UPDATE silver.ingest_progress
        SET status        = 'failed',
            current_step  = 'failed',
            current_stage = COALESCE($2, current_stage),
            failed_at     = now(),
            updated_at    = now(),
            error_text    = $3
        WHERE run_id = $1::uuid
          AND status NOT IN ('completed','failed','cancelled','timed_out')
        RETURNING run_id, triggered_by,
                  EXTRACT(EPOCH FROM (now() - started_at))::float AS duration_seconds
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, run_id, stage, (error or "")[:2000])
        transitioned = row is not None
        if not transitioned:
            log.info("progress.mark_failed: no-op (already terminal) run=%s", run_id)
            return False
        _record_terminal_metrics(
            status="failed",
            triggered_by=row["triggered_by"] or "upload",
            duration_seconds=float(row["duration_seconds"] or 0.0),
        )
        return True
    except Exception as e:
        log.warning("progress.mark_failed failed (run=%s): %s", run_id, e)
        return False


async def mark_timed_out(*, run_id: str, reason: str = "stale_heartbeat") -> bool:
    """Terminal write — sets status=timed_out via conditional update.

    Called by the 15-min stale_run_detector cron when a row has been in
    'started' state without a recent heartbeat.
    """
    sql = """
        UPDATE silver.ingest_progress
        SET status        = 'timed_out',
            current_step  = 'failed',
            failed_at     = now(),
            updated_at    = now(),
            error_text    = $2
        WHERE run_id = $1::uuid
          AND status NOT IN ('completed','failed','cancelled','timed_out')
        RETURNING run_id, triggered_by,
                  EXTRACT(EPOCH FROM (now() - started_at))::float AS duration_seconds
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                run_id,
                f'{{"reason":"{reason}","detected_by":"stale_run_sweep"}}',
            )
        if row is None:
            return False
        _record_terminal_metrics(
            status="timed_out",
            triggered_by=row["triggered_by"] or "upload",
            duration_seconds=float(row["duration_seconds"] or 0.0),
        )
        # Counter for the dedicated stale-run alert.
        try:
            from app.metrics import INGESTION_STALE_RUNS_TOTAL
            INGESTION_STALE_RUNS_TOTAL.inc()
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning("progress.mark_timed_out failed (run=%s): %s", run_id, e)
        return False


async def mark_cancelled(*, run_id: str, reason: str = "user_cancelled") -> bool:
    """Terminal write — sets status=cancelled. Used by the on_failure_task hook
    when Hatchet cancels a workflow (concurrency expiry, explicit cancel)."""
    sql = """
        UPDATE silver.ingest_progress
        SET status        = 'cancelled',
            current_step  = 'failed',
            failed_at     = now(),
            updated_at    = now(),
            error_text    = $2
        WHERE run_id = $1::uuid
          AND status NOT IN ('completed','failed','cancelled','timed_out')
        RETURNING run_id
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, run_id, reason[:2000])
        return row is not None
    except Exception as e:
        log.warning("progress.mark_cancelled failed (run=%s): %s", run_id, e)
        return False


async def lookup_active_run_id(
    *,
    workspace_id: str,
    minio_key: str,
) -> str | None:
    """Return the run_id of the active (non-terminal) row for this file, if any.

    Used by the backward-compat shims below — task code that only knows
    (workspace_id, minio_key) can resolve to the per-run id without
    threading it through every workflow output.
    """
    sql = """
        SELECT run_id::text AS run_id
        FROM silver.ingest_progress
        WHERE workspace_id = $1::uuid AND minio_key = $2
          AND status NOT IN ('completed','failed','cancelled','timed_out')
        ORDER BY attempt_number DESC, started_at DESC
        LIMIT 1
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, workspace_id, minio_key)
        return row["run_id"] if row else None
    except Exception as e:
        log.warning("progress.lookup_active_run_id failed (key=%s): %s", minio_key, e)
        return None


async def get_run(*, run_id: str) -> dict | None:
    """Read a single run row by run_id. Used by the on_failure_task hook to
    resolve current_stage when reporting the failure upstream."""
    sql = """
        SELECT
            run_id::text AS run_id,
            workspace_id::text AS workspace_id,
            project_id::text AS project_id,
            minio_key,
            filename,
            status,
            current_stage,
            current_step,
            step_index,
            total_steps,
            started_at,
            completed_at,
            failed_at,
            error_text,
            attempt_number,
            triggered_by,
            parent_run_id::text AS parent_run_id,
            recovery_reason
        FROM silver.ingest_progress
        WHERE run_id = $1::uuid
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, run_id)
        return dict(row) if row else None
    except Exception as e:
        log.warning("progress.get_run failed (run=%s): %s", run_id, e)
        return None


# ---------------------------------------------------------------------------
# Backward-compat shims — keep existing ingest_pdf.py / tiff_normalize.py
# call sites working until they're migrated to the per-run API.
# ---------------------------------------------------------------------------
async def mark_started(
    *,
    workspace_id: str,
    project_id: str,
    minio_key: str,
    step: str,
    workflow_run_id: str | None = None,
) -> None:
    """LEGACY shim — resolves to the active run_id and calls mark_stage_started.

    If no active run exists (first call for this file), creates one with
    triggered_by='upload'. This preserves the original "one helper, one
    side effect" contract while threading through the per-run schema.
    """
    run_id = await lookup_active_run_id(workspace_id=workspace_id, minio_key=minio_key)
    if run_id is None:
        run_id = await start_run(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=minio_key,
            workflow_run_id=workflow_run_id,
        )
        if run_id is None:
            return  # DB failure — best-effort
    await mark_stage_started(run_id=run_id, stage=step)


async def mark_completed(
    *,
    workspace_id: str,
    minio_key: str,
    report_id: str | None = None,
) -> None:
    """LEGACY shim — resolves to the active run_id and calls
    mark_completed_by_run. Preserves the existing ingest_pdf.py /
    tiff_normalize.py call signature.
    """
    run_id = await lookup_active_run_id(workspace_id=workspace_id, minio_key=minio_key)
    if run_id is None:
        log.warning(
            "progress.mark_completed (legacy): no active run for (ws=%s, key=%s) — skipping",
            workspace_id, minio_key,
        )
        return
    await mark_completed_by_run(run_id=run_id, report_id=report_id)


async def mark_failed(
    *,
    workspace_id: str,
    minio_key: str,
    error: str,
    stage: str | None = None,
) -> None:
    """LEGACY shim — resolves to the active run_id and calls
    mark_failed_by_run. Preserves the existing ingest_pdf.py /
    tiff_normalize.py call signature.
    """
    run_id = await lookup_active_run_id(workspace_id=workspace_id, minio_key=minio_key)
    if run_id is None:
        log.warning(
            "progress.mark_failed (legacy): no active run for (ws=%s, key=%s) — skipping",
            workspace_id, minio_key,
        )
        return
    await mark_failed_by_run(run_id=run_id, stage=stage, error=error)
