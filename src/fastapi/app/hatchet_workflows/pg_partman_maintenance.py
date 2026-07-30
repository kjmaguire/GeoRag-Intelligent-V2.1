"""Nightly pg_partman partition maintenance.

The audit, workflow, and usage ledgers are monthly-partitioned with three
future partitions premade. This durable Hatchet cron advances those partitions
before the premade window expires.

Schedule: 04:15 UTC nightly, matching the retired Ofelia job.
"""

from __future__ import annotations

import logging
import time

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel

from app.audit import emit_audit
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.backup_postgres import _build_dsn

log = logging.getLogger("georag.hatchet.pg_partman_maintenance")


class PgPartmanMaintenanceInput(BaseModel):
    """No-input cron payload; retained for manual Hatchet invocation."""


class PgPartmanMaintenanceOutput(BaseModel):
    status: str
    managed_parent_count: int
    duration_ms: int


pg_partman_maintenance = hatchet.workflow(
    name="pg_partman_maintenance",
    on_crons=["15 4 * * *"],
    input_validator=PgPartmanMaintenanceInput,
)


@pg_partman_maintenance.task(execution_timeout="25m", retries=1)
async def run_pg_partman_maintenance(
    input: PgPartmanMaintenanceInput,
    ctx: Context,
) -> PgPartmanMaintenanceOutput:
    """Advance every parent registered in ``partman.part_config``."""
    _ = input, ctx
    started = time.monotonic()
    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        managed_parent_count = int(
            await conn.fetchval("SELECT count(*) FROM partman.part_config") or 0
        )
        await conn.execute("CALL partman.run_maintenance_proc()")
        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            await emit_audit(
                conn,
                action_type="postgres.partman_maintenance.completed",
                workspace_id=None,
                actor_id=None,
                actor_kind="workflow",
                target_schema="partman",
                target_table="part_config",
                target_id=None,
                payload={
                    "managed_parent_count": managed_parent_count,
                    "duration_ms": duration_ms,
                },
            )
        except Exception:  # pragma: no cover - maintenance must survive audit failure
            log.exception("pg_partman maintenance succeeded but audit emission failed")

        log.info(
            "pg_partman maintenance completed parents=%d duration_ms=%d",
            managed_parent_count,
            duration_ms,
        )
        return PgPartmanMaintenanceOutput(
            status="completed",
            managed_parent_count=managed_parent_count,
            duration_ms=duration_ms,
        )
    finally:
        await conn.close()


__all__ = [
    "PgPartmanMaintenanceInput",
    "PgPartmanMaintenanceOutput",
    "pg_partman_maintenance",
]
