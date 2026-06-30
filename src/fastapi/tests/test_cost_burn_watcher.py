"""§5 — unit + integration tests for the cost-burn watcher."""
from __future__ import annotations

import os
from datetime import UTC

import asyncpg
import pytest

from app.hatchet_workflows import cost_burn_watcher as cbw


# ---------------------------------------------------------------------------
# Workflow contract
# ---------------------------------------------------------------------------
def test_cost_burn_watcher_workflow_registered() -> None:
    assert cbw.cost_burn_watcher is not None
    assert cbw.cost_burn_watcher.name == "cost_burn_watcher"


def test_cost_burn_watcher_in_ai_pool() -> None:
    from app.hatchet_workflows.worker import POOLS
    names = {wf.name for wf in POOLS["ai"]}
    assert "cost_burn_watcher" in names


# ---------------------------------------------------------------------------
# Input model contracts
# ---------------------------------------------------------------------------
def test_input_defaults() -> None:
    inp = cbw.CostBurnWatcherInput()
    assert inp.window_minutes == 60
    assert inp.default_threshold_usd == 5.0


def test_input_window_bounds() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        cbw.CostBurnWatcherInput(window_minutes=14)  # below 15 min floor
    with pytest.raises(ValidationError):
        cbw.CostBurnWatcherInput(window_minutes=1441)  # above 24h ceiling


def test_input_threshold_must_be_positive() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        cbw.CostBurnWatcherInput(default_threshold_usd=0.0)


# ---------------------------------------------------------------------------
# env threshold parsing — defensive
# ---------------------------------------------------------------------------
def test_env_threshold_uses_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COST_BURN_THRESHOLD_USD_PER_HOUR", raising=False)
    assert cbw._env_threshold(5.0) == 5.0


def test_env_threshold_parses_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_BURN_THRESHOLD_USD_PER_HOUR", "10.5")
    assert cbw._env_threshold(5.0) == 10.5


def test_env_threshold_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_BURN_THRESHOLD_USD_PER_HOUR", "not-a-number")
    assert cbw._env_threshold(5.0) == 5.0  # falls back


def test_env_threshold_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero override would emit on the first dollar — defensive default."""
    monkeypatch.setenv("COST_BURN_THRESHOLD_USD_PER_HOUR", "0")
    assert cbw._env_threshold(5.0) == 5.0


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------
def test_output_round_trip() -> None:
    from datetime import datetime
    out = cbw.CostBurnWatcherOutput(
        workspaces_checked=3,
        workspaces_over_threshold=1,
        alerts_emitted=1,
        alerts_suppressed_idempotent=0,
        window_minutes=60,
        sampled_at=datetime.now(tz=UTC),
    )
    d = out.model_dump()
    assert d["alerts_emitted"] == 1


# ===========================================================================
# Integration — live stack
# ===========================================================================
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def workspace_id(pg_conn: asyncpg.Connection) -> str:
    row = await pg_conn.fetchrow(
        "SELECT workspace_id::text AS w FROM silver.workspaces LIMIT 1",
    )
    if row is None:
        pytest.skip("silver.workspaces is empty")
    return row["w"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_threshold_falls_back_to_env_default(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    """A workspace with NO entry in workspace_cost_ceilings should get
    the env default."""
    # Make sure there's no row for this workspace
    await pg_conn.execute(
        "DELETE FROM usage.workspace_cost_ceilings WHERE workspace_id = $1::uuid",
        workspace_id,
    )
    threshold = await cbw._resolve_threshold_for_workspace(
        pg_conn, workspace_id, env_default=7.5,
    )
    assert threshold == 7.5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_threshold_uses_workspace_ceiling(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    """When workspace_cost_ceilings.monthly_ceiling_usd is set, the
    hourly threshold = monthly / 720."""
    await pg_conn.execute(
        """
        INSERT INTO usage.workspace_cost_ceilings (workspace_id, monthly_ceiling_usd)
        VALUES ($1::uuid, 7200.00)
        ON CONFLICT (workspace_id) DO UPDATE SET monthly_ceiling_usd = EXCLUDED.monthly_ceiling_usd
        """,
        workspace_id,
    )
    try:
        threshold = await cbw._resolve_threshold_for_workspace(
            pg_conn, workspace_id, env_default=999.0,
        )
        # 7200 / 720 = 10.0
        assert threshold == 10.0
    finally:
        await pg_conn.execute(
            "DELETE FROM usage.workspace_cost_ceilings WHERE workspace_id = $1::uuid",
            workspace_id,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_suppresses_recent_alert(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    """Emit one cost.burn.alert for the workspace, then assert the
    idempotency check sees it."""
    # Insert a synthetic alert
    audit_row = await pg_conn.fetchrow(
        """
        INSERT INTO audit.audit_ledger
            (workspace_id, actor_id, actor_kind, action_type,
             target_schema, target_table, target_id, payload)
        VALUES
            ($1::uuid, NULL, 'workflow', 'cost.burn.alert',
             'usage', 'usage_events', $1::text,
             '{"severity":"high","spent_usd":50,"threshold_usd":5}'::jsonb)
        RETURNING id
        """,
        workspace_id,
    )
    try:
        has_alert = await cbw._has_recent_unacked_alert(
            pg_conn, workspace_id, window_minutes=60,
        )
        assert has_alert is True

        # Acknowledge it — now the check should return False
        await pg_conn.execute(
            """
            INSERT INTO audit.audit_ledger
                (workspace_id, actor_id, actor_kind, action_type,
                 target_schema, target_table, target_id, payload)
            VALUES
                ($1::uuid, 1, 'user', 'cost.burn.alert.acknowledged',
                 'usage', 'usage_events', $1::text,
                 jsonb_build_object('original_audit_id', $2::text))
            """,
            workspace_id, str(audit_row["id"]),
        )
        has_after = await cbw._has_recent_unacked_alert(
            pg_conn, workspace_id, window_minutes=60,
        )
        assert has_after is False
    finally:
        # Cleanup audit rows
        await pg_conn.execute(
            """
            DELETE FROM audit.audit_ledger
             WHERE workspace_id = $1::uuid
               AND action_type IN ('cost.burn.alert', 'cost.burn.alert.acknowledged')
               AND target_table = 'usage_events'
            """,
            workspace_id,
        )
