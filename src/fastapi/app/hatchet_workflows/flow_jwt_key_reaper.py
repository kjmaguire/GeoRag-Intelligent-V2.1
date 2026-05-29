"""Phase 7 Step 2 (R-P6-2) — nightly Hatchet workflow that reaps
expired ``workflow.flow_jwt_keys`` rows.

Phase 6 Step 3 introduced multi-kid JWT rotation with overlap windows
(per-flow ``flow_jwt_keys`` table, ``valid_until`` timestamps). Nothing
cleaned up after the rotation — rows accumulated forever. This
workflow calls the ``workflow.reap_expired_flow_jwt_keys`` SECURITY
DEFINER function nightly, dropping any row whose ``valid_until`` is
more than ``retention_days`` (default 7) in the past.

Schedule: ``0 4 * * *`` UTC — runs 2h after audit_ledger_verify (02:00)
so we don't pile rotation-related load.
"""

from __future__ import annotations

import os

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


class FlowJwtKeyReaperInput(BaseModel):
    retention_days: int = Field(
        default=7,
        ge=0,
        description="How many days past valid_until a row is kept before reaping.",
    )


class FlowJwtKeyReaperOutput(BaseModel):
    deleted_count: int
    retention_days: int


flow_jwt_key_reaper = hatchet.workflow(
    name="flow_jwt_key_reaper",
    on_crons=["0 4 * * *"],
    input_validator=FlowJwtKeyReaperInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@flow_jwt_key_reaper.task(execution_timeout="2m")
async def reap(input: FlowJwtKeyReaperInput, ctx: Context) -> FlowJwtKeyReaperOutput:
    enc_key = os.environ.get("AUDIT_ENCRYPTION_KEY", "")
    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        async with conn.transaction():
            if enc_key:
                # The reap function doesn't decrypt, but set_config keeps
                # the GUC pattern uniform with the other workflow helpers.
                await conn.execute(
                    "SELECT set_config('app.audit_encryption_key', $1, true)",
                    enc_key,
                )
            row = await conn.fetchrow(
                "SELECT deleted_count FROM workflow.reap_expired_flow_jwt_keys($1)",
                input.retention_days,
            )
    finally:
        await conn.close()

    deleted = int(row["deleted_count"]) if row is not None else 0

    # Phase 5 admin surface push — drives Admin/Integrations rotation_history.
    # Best-effort.
    try:
        from app.services.laravel_bridge import post_admin_surface_updated
        admin_payload = {
            "workflow_kind": "flow_jwt_key_reaper",
            "deleted_count": deleted,
            "retention_days": input.retention_days,
            "status": "success",
        }
        await post_admin_surface_updated(
            surface="workflow-runs",
            affected_props=["workflow_runs"],
            payload=admin_payload,
        )
        await post_admin_surface_updated(
            surface="integrations",
            affected_props=["rotation_history", "flow_jwt_keys"],
            payload=admin_payload,
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "flow_jwt_key_reaper: admin surface broadcasts failed err=%s", exc,
        )

    return FlowJwtKeyReaperOutput(
        deleted_count=deleted,
        retention_days=input.retention_days,
    )


__all__ = [
    "flow_jwt_key_reaper",
    "FlowJwtKeyReaperInput",
    "FlowJwtKeyReaperOutput",
]
