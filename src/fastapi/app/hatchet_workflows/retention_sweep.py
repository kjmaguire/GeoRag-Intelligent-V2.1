"""Nightly retention sweep — audit.query_audit_log + silver.ingest_progress.

2026-08-14 DB audit item M1: neither table had any retention policy, so
both grow unbounded — query_audit_log gains a row per RAG query and
ingest_progress gains a row per ingest attempt (per-run rows since the
2026-05-25 reliability migration, so retries multiply rows per file).

Two purges, one cron:

1. audit.query_audit_log — DELETE rows older than
   ``QUERY_AUDIT_RETENTION_DAYS`` (default 180). NI 43-101 traceability
   only needs a bounded window online; long-term compliance history lives
   in the pg backups (backup_postgres, 02:00 UTC nightly).

2. silver.ingest_progress — DELETE rows in a terminal state older than
   ``INGEST_PROGRESS_RETENTION_DAYS`` (default 90), EXCEPT the newest
   attempt per (workspace_id, minio_key), which is kept regardless of
   age. The IngestionRuns UI's latest-per-file surface
   (silver.ingest_progress_latest_per_file, DISTINCT ON (workspace_id,
   minio_key) ORDER BY attempt_number DESC, started_at DESC) must always
   resolve a row for every file ever ingested — pruning the newest
   attempt would make an old completed file vanish from the UI.
   Terminal states are the four immutable ones from the state machine in
   _progress.py: completed, failed, cancelled, timed_out (the audited
   finding listed three; timed_out is equally terminal and equally
   prunable). parent_run_id links are ON DELETE SET NULL, so deleting an
   old original does not cascade into its recovery chain.

Both purges delete in batches of ``RETENTION_SWEEP_BATCH_SIZE`` (default
5000) and loop until a batch comes back short, so a first run against a
year of backlog cannot hold a single long transaction or bloat WAL in
one burst.

Tenancy note: this is cross-tenant maintenance, so NO workspace GUC is
bound on the connection. The pool connects as the table-owning ``georag``
role, which bypasses RLS (and several RLS policies on these tables are
fail-open when app.workspace_id is unset anyway — see
[[legacy-guc-writers-audit-2026-05-28]]). That is relied upon here and
should eventually move to an explicit BYPASSRLS maintenance role instead
of leaning on owner-bypass / fail-open behaviour.

Cron: 04:45 UTC nightly — after cold_tier_archive (04:00),
pg_partman_maintenance + idempotency_keys_cleanup (04:15) and
enrich_passage_context (04:30), before the embed sweep (05:45), so the
DELETEs don't contend with the other nightly writers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _t
from datetime import UTC, datetime

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import _progress, hatchet

log = logging.getLogger("georag.hatchet.retention_sweep")

# Hard ceiling on batches per table per run — a backstop against a
# pathological loop (e.g. a clock problem making the cutoff never
# converge). 2000 batches x 5000 rows = 10M rows per night, far beyond
# any realistic backlog.
_MAX_BATCHES = 2000


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class RetentionSweepInput(BaseModel):
    """Optional overrides — left empty for the cron path."""

    query_audit_retention_days: int | None = Field(
        default=None,
        description="Override QUERY_AUDIT_RETENTION_DAYS for this run.",
    )
    ingest_progress_retention_days: int | None = Field(
        default=None,
        description="Override INGEST_PROGRESS_RETENTION_DAYS for this run.",
    )


class RetentionSweepOut(BaseModel):
    query_audit_rows_deleted: int
    ingest_progress_rows_deleted: int
    query_audit_retention_days: int
    ingest_progress_retention_days: int
    duration_ms: int
    swept_at: str  # ISO-8601 UTC


_QUERY_AUDIT_BATCH_SQL = """
    WITH victims AS (
        SELECT audit_id
        FROM audit.query_audit_log
        WHERE created_at < now() - ($1::int * interval '1 day')
        LIMIT $2::int
    ),
    d AS (
        DELETE FROM audit.query_audit_log
        WHERE audit_id IN (SELECT audit_id FROM victims)
        RETURNING 1
    )
    SELECT count(*) FROM d
"""

# rn = 1 is the row the latest-per-file view resolves for this file —
# never deleted, whatever its age. Everything ranked behind it is fair
# game once terminal + past the cutoff (age measured from the terminal
# timestamp, falling back to updated_at for legacy rows).
_INGEST_PROGRESS_BATCH_SQL = """
    WITH ranked AS (
        SELECT run_id,
               ROW_NUMBER() OVER (
                   PARTITION BY workspace_id, minio_key
                   ORDER BY attempt_number DESC, started_at DESC
               ) AS rn
        FROM silver.ingest_progress
    ),
    victims AS (
        SELECT p.run_id
        FROM silver.ingest_progress p
        JOIN ranked r ON r.run_id = p.run_id
        WHERE r.rn > 1
          AND p.status = ANY($3::text[])
          AND COALESCE(p.completed_at, p.failed_at, p.updated_at)
                < now() - ($1::int * interval '1 day')
        LIMIT $2::int
    ),
    d AS (
        DELETE FROM silver.ingest_progress
        WHERE run_id IN (SELECT run_id FROM victims)
        RETURNING 1
    )
    SELECT count(*) FROM d
"""


async def _delete_in_batches(
    sql: str,
    *args: object,
    batch_size: int,
    label: str,
) -> int:
    """Run one batched-DELETE statement until a batch comes back short.

    Each batch is its own transaction (pool-acquired autocommit
    statement), so locks and WAL are released between batches and a
    mid-run crash leaves prior batches durably deleted.
    """
    pool = await _progress.get_pool()
    total = 0
    for _ in range(_MAX_BATCHES):
        async with pool.acquire() as conn:
            deleted = int(await conn.fetchval(sql, *args) or 0)
        total += deleted
        if deleted < batch_size:
            return total
        # Yield between batches so heartbeat/progress writes sharing the
        # pool aren't starved during a large backlog drain.
        await asyncio.sleep(0.1)
    log.warning(
        "retention_sweep: %s hit the %d-batch ceiling (deleted %d rows); "
        "remainder rolls over to tomorrow's run",
        label, _MAX_BATCHES, total,
    )
    return total


retention_sweep = hatchet.workflow(
    name="retention_sweep",
    on_crons=["45 4 * * *"],  # 04:45 UTC nightly — see module docstring
    input_validator=RetentionSweepInput,
)


@retention_sweep.task(execution_timeout="55m", retries=0)
async def run_retention_sweep(
    input: RetentionSweepInput, ctx: Context,
) -> RetentionSweepOut:
    """Purge expired query_audit_log + terminal ingest_progress rows."""
    t0 = _t.monotonic()

    audit_days = (
        input.query_audit_retention_days
        if input.query_audit_retention_days
        else _int_env("QUERY_AUDIT_RETENTION_DAYS", 180)
    )
    progress_days = (
        input.ingest_progress_retention_days
        if input.ingest_progress_retention_days
        else _int_env("INGEST_PROGRESS_RETENTION_DAYS", 90)
    )
    batch_size = _int_env("RETENTION_SWEEP_BATCH_SIZE", 5000)

    audit_deleted = await _delete_in_batches(
        _QUERY_AUDIT_BATCH_SQL,
        audit_days,
        batch_size,
        batch_size=batch_size,
        label="audit.query_audit_log",
    )
    progress_deleted = await _delete_in_batches(
        _INGEST_PROGRESS_BATCH_SQL,
        progress_days,
        batch_size,
        list(_progress.TERMINAL_STATUSES),
        batch_size=batch_size,
        label="silver.ingest_progress",
    )

    duration_ms = int((_t.monotonic() - t0) * 1000)
    swept_at = datetime.now(UTC)

    try:
        pool = await _progress.get_pool()
        await emit_audit(
            pool,
            action_type="retention_sweep.complete",
            actor_kind="workflow",
            target_schema="audit",
            target_table="query_audit_log",
            target_id=None,
            payload={
                "query_audit_rows_deleted": audit_deleted,
                "ingest_progress_rows_deleted": progress_deleted,
                "query_audit_retention_days": audit_days,
                "ingest_progress_retention_days": progress_days,
                "duration_ms": duration_ms,
                "swept_at": swept_at.isoformat(),
            },
        )
    except Exception:  # pragma: no cover — never fail the sweep on audit-write
        log.exception("emit_audit failed (retention sweep itself succeeded)")

    out = RetentionSweepOut(
        query_audit_rows_deleted=audit_deleted,
        ingest_progress_rows_deleted=progress_deleted,
        query_audit_retention_days=audit_days,
        ingest_progress_retention_days=progress_days,
        duration_ms=duration_ms,
        swept_at=swept_at.isoformat(),
    )
    log.info("retention_sweep complete: %s", out.model_dump())
    return out


__all__ = ["retention_sweep", "RetentionSweepInput", "RetentionSweepOut"]
