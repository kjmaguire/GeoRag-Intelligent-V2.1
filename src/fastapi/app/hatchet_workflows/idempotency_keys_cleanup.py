"""Nightly cleanup of expired workspace.idempotency_keys rows.

Phase 0 #§35.1 closure (v2.0 deep-eval Dim 4 -1pt).

The @georag_agent wrapper writes idempotency rows with a TTL (30d for R2,
90d for R3+ — see src/fastapi/app/agents/wrapper.py:_idempotency_store).
Without periodic cleanup the table grows unbounded — every R2+ agent
invocation adds a row. This workflow runs nightly and drops rows whose
`expires_at < now()`.

The workflow is intentionally NOT marked as a @georag_agent: it's pure
operational housekeeping with no decisional cognition. Hatchet-scheduled,
no LangGraph, no LLM calls. Audit-trailed via emit_audit so ops can see
"X rows expired tonight" in the audit ledger.

Cron: 04:15 UTC nightly. Stagger from audit_ledger_verify (02:00) and
storage_tiering_run (03:00) so the workspace.* writes don't contend.
"""
from __future__ import annotations

import logging
import os
import time as _t
from datetime import datetime, timezone

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.idempotency_keys_cleanup")


class CleanupInput(BaseModel):
    """Optional override — left empty for the cron path."""

    older_than_days: int | None = Field(
        default=None,
        description=(
            "When set, deletes rows older than N days REGARDLESS of "
            "expires_at. Use sparingly — bulk archival, not regular ops."
        ),
    )


class CleanupOut(BaseModel):
    rows_deleted: int
    duration_ms: int
    cutoff_at: str  # ISO-8601 UTC


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


idempotency_keys_cleanup = hatchet.workflow(
    name="idempotency_keys_cleanup",
    on_crons=["15 4 * * *"],  # 04:15 UTC nightly
    input_validator=CleanupInput,
)


@idempotency_keys_cleanup.task(execution_timeout="25m", retries=0)
async def run_cleanup(input: CleanupInput, ctx: Context) -> CleanupOut:
    """Drop rows whose TTL has passed (regular path) or whose age exceeds
    `older_than_days` (bulk archival path)."""
    t0 = _t.monotonic()
    inp = input

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
    try:
        async with pool.acquire() as conn:
            if inp.older_than_days is not None:
                # Bulk-archival path — caller takes responsibility.
                cutoff = datetime.now(timezone.utc)
                deleted = await conn.fetchval(
                    """
                    WITH d AS (
                        DELETE FROM workspace.idempotency_keys
                         WHERE created_at < now() - ($1::int * interval '1 day')
                         RETURNING 1
                    )
                    SELECT count(*) FROM d
                    """,
                    inp.older_than_days,
                )
            else:
                # Regular path — drop rows whose TTL has passed.
                cutoff = datetime.now(timezone.utc)
                deleted = await conn.fetchval(
                    """
                    WITH d AS (
                        DELETE FROM workspace.idempotency_keys
                         WHERE expires_at IS NOT NULL
                           AND expires_at < now()
                         RETURNING 1
                    )
                    SELECT count(*) FROM d
                    """,
                )

            duration_ms = int((_t.monotonic() - t0) * 1000)
            try:
                await emit_audit(
                    conn,
                    action_type="idempotency_keys.cleanup.complete",
                    actor_kind="workflow",
                    target_schema="workspace",
                    target_table="idempotency_keys",
                    target_id=None,
                    payload={
                        "rows_deleted": int(deleted or 0),
                        "duration_ms": duration_ms,
                        "cutoff_at": cutoff.isoformat(),
                        "older_than_days": inp.older_than_days,
                    },
                )
            except Exception:  # pragma: no cover — never fail cleanup on audit-write
                log.exception("emit_audit failed (cleanup itself succeeded)")
    finally:
        await pool.close()

    out = CleanupOut(
        rows_deleted=int(deleted or 0),
        duration_ms=duration_ms,
        cutoff_at=cutoff.isoformat(),
    )
    log.info("idempotency_keys_cleanup complete: %s", out.model_dump())
    return out


__all__ = ["idempotency_keys_cleanup", "CleanupInput", "CleanupOut"]
