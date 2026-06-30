"""Phase 0 step 4.2 — nightly Hatchet workflow for audit-ledger hash-chain verification.

The verification logic itself lives in pure SQL (``audit.run_verification``,
deployed by ``database/raw/phase0/100-audit-verify-function.sql``). This
workflow only computes the time window, calls the function, reads back the
inserted ``audit.audit_ledger_verification_runs`` row, and surfaces
``status`` / ``rows_verified`` to Hatchet for observability.

Schedule: ``0 2 * * *`` UTC nightly (Tenant Isolation Auditor pattern from
the kickoff). Manually invokable via ``audit_ledger_verify.run({})``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


class AuditVerifyInput(BaseModel):
    """Optional override for the verification window. Default is the last 24 h."""

    start_at: datetime | None = Field(
        default=None, description="UTC start of the window (inclusive)."
    )
    end_at: datetime | None = Field(
        default=None, description="UTC end of the window (exclusive)."
    )


class AuditVerifyOutput(BaseModel):
    run_id: str
    status: str
    rows_verified: int
    window_start: datetime
    window_end: datetime


audit_ledger_verify = hatchet.workflow(
    name="audit_ledger_verify",
    on_crons=["0 2 * * *"],
    input_validator=AuditVerifyInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@audit_ledger_verify.task(execution_timeout="5m")
async def run_verification(input: AuditVerifyInput, ctx: Context) -> AuditVerifyOutput:
    end_at = input.end_at or datetime.now(tz=UTC)
    start_at = input.start_at or (end_at - timedelta(days=1))

    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        run_id: UUID = await conn.fetchval(
            "SELECT audit.run_verification($1::timestamptz, $2::timestamptz, NULL)",
            start_at,
            end_at,
        )
        row = await conn.fetchrow(
            """
            SELECT status, rows_verified
              FROM audit.audit_ledger_verification_runs
             WHERE id = $1
            """,
            run_id,
        )
    finally:
        await conn.close()

    if row is None:  # pragma: no cover — RETURNING-equivalent path
        return AuditVerifyOutput(
            run_id=str(run_id),
            status="error",
            rows_verified=0,
            window_start=start_at,
            window_end=end_at,
        )

    # Phase 5 admin surface push — drives Admin/AuditExplorer.
    # Only fires on the successful-completion path (skip the error
    # short-circuit above where row is None). Best-effort.
    try:
        from app.services.laravel_bridge import post_admin_surface_updated
        admin_payload = {
            "workflow_kind": "audit_ledger_verify",
            "run_id": str(run_id),
            "status": row["status"],
            "rows_verified": int(row["rows_verified"]),
        }
        await post_admin_surface_updated(
            surface="workflow-runs",
            affected_props=["workflow_runs"],
            payload=admin_payload,
        )
        await post_admin_surface_updated(
            surface="audit-explorer",
            affected_props=["entries"],
            payload=admin_payload,
        )
        # Phase 6 — Dashboards/EvidenceQuality reads from the same
        # audit_ledger feed (rejection-reason rollups from silver.answer_runs).
        # Audit verify runs are the natural cadence for refreshing it.
        await post_admin_surface_updated(
            surface="dashboards-evidence-quality",
            affected_props=["totals", "by_day", "rejection_reasons"],
            payload=admin_payload,
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "audit_ledger_verify: admin surface broadcasts failed run_id=%s err=%s",
            run_id, exc,
        )

    return AuditVerifyOutput(
        run_id=str(run_id),
        status=row["status"],
        rows_verified=int(row["rows_verified"]),
        window_start=start_at,
        window_end=end_at,
    )


__all__ = ["audit_ledger_verify", "AuditVerifyInput", "AuditVerifyOutput"]
