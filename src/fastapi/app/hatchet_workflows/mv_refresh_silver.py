"""Phase 15 Step 1 (R-P14-2) — nightly refresh of the agent's silver
materialised views.

The agent's NUMERIC prompt path reads `silver.mv_collar_summary` to
populate the HIGH-CONFIDENCE SUMMARIES block. Phase 14 root-caused
R-P13-1 to a stale MV — Dagster's ingestion pipeline is expected to
refresh after every batch, but in dev / paused-Dagster environments
the MV drifts back to empty.

This workflow calls the `workflow.refresh_silver_agent_mvs()`
SECURITY DEFINER function nightly. Same pattern as
`flow_jwt_key_reaper` (Phase 7 Step 2) — AI pool, cron-triggered,
asyncpg + direct postgres connection.

Schedule: `0 3 * * *` UTC — between audit_ledger_verify (02:00)
and flow_jwt_key_reaper (04:00), so the three nightly maintenance
workflows fan out across the small hours.
"""

from __future__ import annotations

import os

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


class MvRefreshSilverInput(BaseModel):
    # Reserved for future per-MV opt-in / opt-out lists; today the
    # workflow refreshes the full set the SQL function knows about.
    forced: bool = Field(
        default=False,
        description="If True, log at INFO even when no MVs changed. Off by default.",
    )


class MvRefreshSilverOutput(BaseModel):
    refreshed_count: int


mv_refresh_silver = hatchet.workflow(
    name="mv_refresh_silver",
    on_crons=["0 3 * * *"],
    input_validator=MvRefreshSilverInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@mv_refresh_silver.task(execution_timeout="5m")
async def refresh(input: MvRefreshSilverInput, ctx: Context) -> MvRefreshSilverOutput:
    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        rows = await conn.fetch(
            "SELECT mv_name FROM workflow.refresh_silver_agent_mvs()",
        )
    finally:
        await conn.close()

    return MvRefreshSilverOutput(refreshed_count=len(rows))


__all__ = [
    "mv_refresh_silver",
    "MvRefreshSilverInput",
    "MvRefreshSilverOutput",
]
