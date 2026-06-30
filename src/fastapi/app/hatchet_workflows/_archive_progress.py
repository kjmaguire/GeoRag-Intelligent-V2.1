"""Per-archive ingestion progress writer (Theme D — 2026-06-03 audit).

Parallel of ``_progress.py`` for the ``ingest_zip_archive`` workflow.
Writes a single parent row per ZIP upload into
``silver.archive_ingest_runs``; child per-file rows live in
``silver.ingest_progress`` with the new ``archive_run_id`` FK so
operators can drill from "this archive failed" → "these 4 specific
PDFs inside it crashed".

State machine
-------------
::

    queued      → extracting   : workflow picked up the ZIP
    extracting  → fanning_out  : zip extracted, dispatching per-file
    fanning_out → completed    : every child reached terminal OK
    fanning_out → partial      : some children completed, some failed
    *           → failed       : workflow crashed (on_failure_task)
    *           → cancelled    : Hatchet cancelled before workflow ran

Terminal states: ``completed``, ``failed``, ``partial``, ``cancelled``.
The terminal-write helpers use the conditional-update pattern so a
delayed worker can't overwrite a previously-set terminal state — same
guarantee as ``_progress.mark_failed_by_run``.

Best-effort error handling
--------------------------
Every helper swallows DB errors and logs at WARN — the surrounding
workflow task must not crash because progress tracking failed. The
Hatchet ``on_failure_task`` is the durable backstop; missing progress
writes just mean the IngestionRuns UI lags by a tick.
"""
from __future__ import annotations

import contextlib
import logging
import os
import uuid

import asyncpg

log = logging.getLogger("georag.hatchet.archive_progress")


TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "partial", "cancelled")
ALLOWED_TRIGGERS: tuple[str, ...] = ("upload", "manual_retry", "cron_recovery")


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


# Module-level asyncpg pool — mirrors _progress.py. Hatchet worker is
# single-process so one pool is safe + avoids per-call connect overhead.
_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool.is_closing():
        _pool = await asyncpg.create_pool(
            _dsn(),
            min_size=1,
            max_size=2,
            statement_cache_size=0,
        )
    return _pool


def _filename_from_key(minio_key: str) -> str:
    """Extract a human-readable filename from a MinIO/SeaweedFS key."""
    return minio_key.rsplit("/", 1)[-1] if "/" in minio_key else minio_key


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


async def start_run(
    *,
    workspace_id: str,
    project_id: str,
    minio_key: str,
    run_id: str,
    triggered_by: str = "upload",
    workflow_run_id: str | None = None,
) -> str | None:
    """Insert the parent archive_ingest_runs row and return archive_run_id.

    ``run_id`` is the caller-supplied correlation id (matches
    ``IngestZipArchiveInput.run_id``) — used as the natural key so
    the workflow can find its own row on retries / re-entry without
    threading archive_run_id through Hatchet steps.

    Idempotent on the run_id UNIQUE constraint — re-running the same
    workflow returns the existing archive_run_id rather than blowing
    up on conflict.

    Returns None on DB failure (best-effort).
    """
    if triggered_by not in ALLOWED_TRIGGERS:
        log.warning(
            "archive_progress.start_run: unknown triggered_by=%r (forcing 'upload')",
            triggered_by,
        )
        triggered_by = "upload"

    new_archive_run_id = str(uuid.uuid4())
    filename = _filename_from_key(minio_key)

    sql = """
        INSERT INTO silver.archive_ingest_runs (
            archive_run_id, workspace_id, project_id, run_id,
            minio_key, filename, status,
            triggered_by, workflow_run_id,
            started_at, updated_at
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4::uuid,
            $5, $6, 'queued',
            $7, $8,
            now(), now()
        )
        ON CONFLICT (run_id) DO UPDATE
          SET updated_at = now()  -- harmless touch so RETURNING fires
        RETURNING archive_run_id::text
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                new_archive_run_id, workspace_id, project_id, run_id,
                minio_key, filename,
                triggered_by, workflow_run_id,
            )
        return row["archive_run_id"] if row else new_archive_run_id
    except Exception as e:
        log.warning(
            "archive_progress.start_run failed (run_id=%s, key=%s): %s",
            run_id, minio_key, e,
        )
        return None


async def mark_extracting(*, archive_run_id: str, file_count: int | None = None) -> None:
    """Transition queued → extracting; optionally record discovered file_count.

    No-op when status is already terminal.
    """
    sql = """
        UPDATE silver.archive_ingest_runs
        SET status      = 'extracting',
            file_count  = COALESCE($2, file_count),
            updated_at  = now()
        WHERE archive_run_id = $1::uuid
          AND status NOT IN ('completed', 'failed', 'partial', 'cancelled')
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql, archive_run_id, file_count)
    except Exception as e:
        log.warning("archive_progress.mark_extracting failed (run=%s): %s", archive_run_id, e)


async def mark_fanning_out(*, archive_run_id: str, file_count: int) -> None:
    """Transition extracting → fanning_out and pin the file_count.

    file_count is the authoritative count of extracted files (after
    junk-file filtering, before per-file dispatch).
    """
    sql = """
        UPDATE silver.archive_ingest_runs
        SET status      = 'fanning_out',
            file_count  = $2,
            updated_at  = now()
        WHERE archive_run_id = $1::uuid
          AND status NOT IN ('completed', 'failed', 'partial', 'cancelled')
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql, archive_run_id, file_count)
    except Exception as e:
        log.warning("archive_progress.mark_fanning_out failed (run=%s): %s", archive_run_id, e)


async def increment_counts(
    *,
    archive_run_id: str,
    succeeded: int = 0,
    failed: int = 0,
    skipped: int = 0,
) -> None:
    """Bump per-file outcome counts on the parent. Atomic add."""
    if succeeded == 0 and failed == 0 and skipped == 0:
        return
    sql = """
        UPDATE silver.archive_ingest_runs
        SET files_succeeded = files_succeeded + $2,
            files_failed    = files_failed    + $3,
            files_skipped   = files_skipped   + $4,
            updated_at      = now()
        WHERE archive_run_id = $1::uuid
          AND status NOT IN ('completed', 'failed', 'cancelled')
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql, archive_run_id, succeeded, failed, skipped)
    except Exception as e:
        log.warning("archive_progress.increment_counts failed (run=%s): %s", archive_run_id, e)


async def mark_terminal(
    *,
    archive_run_id: str,
    status: str,
    error_text: str | None = None,
) -> bool:
    """Terminal write — conditional update so a delayed worker can't
    re-open a previously-closed run.

    Returns True iff the row actually transitioned.
    """
    if status not in TERMINAL_STATUSES:
        log.warning("archive_progress.mark_terminal: invalid status %r", status)
        return False

    # 'completed' vs 'partial' decided by caller (knows the file counts).
    sql = """
        UPDATE silver.archive_ingest_runs
        SET status        = $2,
            error_text    = COALESCE($3, error_text),
            completed_at  = CASE WHEN $2 IN ('completed','partial') THEN now() ELSE completed_at END,
            failed_at     = CASE WHEN $2 IN ('failed','cancelled')  THEN now() ELSE failed_at END,
            updated_at    = now()
        WHERE archive_run_id = $1::uuid
          AND status NOT IN ('completed', 'failed', 'partial', 'cancelled')
        RETURNING archive_run_id
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, archive_run_id, status, (error_text or "")[:2000])
        return row is not None
    except Exception as e:
        log.warning(
            "archive_progress.mark_terminal failed (run=%s, status=%s): %s",
            archive_run_id, status, e,
        )
        return False


async def lookup_archive_run_id_by_run_id(run_id: str) -> str | None:
    """Resolve archive_run_id from the caller-supplied run_id correlation key.

    Used by the on_failure_task hook: the input model carries run_id
    (not archive_run_id) so failure handlers need a lookup.
    """
    sql = """
        SELECT archive_run_id::text AS archive_run_id
        FROM silver.archive_ingest_runs
        WHERE run_id = $1::uuid
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, run_id)
        return row["archive_run_id"] if row else None
    except Exception as e:
        log.warning(
            "archive_progress.lookup_archive_run_id_by_run_id failed (run_id=%s): %s",
            run_id, e,
        )
        return None


# ---------------------------------------------------------------------------
# Convenience: a context manager that calls start_run + ensures we mark
# the run terminal on any unhandled exception in the workflow body.
# Used by ingest_zip_archive.run_zip_ingest.
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def archive_lifecycle(
    *,
    workspace_id: str,
    project_id: str,
    minio_key: str,
    run_id: str,
    triggered_by: str = "upload",
    workflow_run_id: str | None = None,
):
    """Yield archive_run_id; on raise, mark the run failed with the
    exception text.

    Body still has to call mark_extracting / mark_fanning_out /
    increment_counts / mark_terminal('completed' | 'partial') —
    this just guarantees the FAILURE path closes the row even if
    the body raises before the explicit terminal write.

    archive_run_id may be None (DB failed at start_run); body must
    tolerate that case (treat helpers as no-ops downstream).
    """
    archive_run_id = await start_run(
        workspace_id=workspace_id,
        project_id=project_id,
        minio_key=minio_key,
        run_id=run_id,
        triggered_by=triggered_by,
        workflow_run_id=workflow_run_id,
    )
    try:
        yield archive_run_id
    except Exception as exc:  # noqa: BLE001 — explicitly broad
        if archive_run_id is not None:
            await mark_terminal(
                archive_run_id=archive_run_id,
                status="failed",
                error_text=f"{type(exc).__name__}: {exc}",
            )
        raise
