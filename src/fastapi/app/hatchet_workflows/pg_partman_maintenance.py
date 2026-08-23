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
from app.db.dsn import build_dsn as _build_dsn
from app.hatchet_workflows import hatchet

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

        if managed_parent_count == 0:
            # A maintenance job that reports success while managing
            # nothing is worse than one that fails: it produces a green
            # nightly signal for a subsystem that is entirely inert.
            #
            # This is the live state on Azure. partman.create_parent() is
            # called only from database/raw/phase0/{20,30,60}, and the raw
            # SQL layer has never been applied there — CD runs `artisan
            # migrate` only. So audit.audit_ledger, workflow.workflow_runs
            # and usage.usage_events exist as plain, unpartitioned heaps,
            # `CALL partman.run_maintenance_proc()` is a no-op, and there
            # is no monthly rollover and no partition-level retention.
            # Log Analytics has the proof: hatchet-worker-cc,
            # 2026-08-18T04:15:02Z, "pg_partman maintenance complete"
            # with parents=0.
            #
            # Reported, not raised. The partition conversion is a
            # deliberate open decision (partman cannot adopt a plain
            # table, so it needs create-new + INSERT...SELECT + rename on
            # the live audit ledger), and turning a known, accepted state
            # into a nightly hard failure is the alarm-fatigue pattern
            # that already made georag-pg-cc-down worthless. What matters
            # is that the status stops SAYING "completed" — a caller or a
            # dashboard reading this output now sees the truth.
            #
            # retention_sweep and cold_tier_archive rest on the same
            # assumption and should get the same treatment.
            #
            # When the decision is made: either register the parents
            # (database/raw/phase0/{20,30,60}) or retire this workflow in
            # favour of delete-by-range retention on plain tables.
            log.error(
                "pg_partman maintenance managed ZERO parents — "
                "partman.part_config is empty, so this job did nothing. "
                "audit.audit_ledger, workflow.workflow_runs and "
                "usage.usage_events are growing as unbounded heaps with no "
                "rollover and no partition-level retention. "
                "duration_ms=%d",
                duration_ms,
            )
            return PgPartmanMaintenanceOutput(
                status="inert_no_parents_registered",
                managed_parent_count=0,
                duration_ms=duration_ms,
            )

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
