"""cost_burn_watcher: the 2x hard stop must not depend on the alert.

The suspension used to sit AFTER ``if already_alerted: continue``. The first
over-threshold reading (say 1.2x) wrote the alert; from then until somebody
acknowledged it, both the alert and the suspension were suppressed, so a
workspace could climb from 1.2x to 10x with the hard stop unreachable.

Separately, ``_suspend_workspace`` updated ``usage.workspace_cost_ceilings``
and returned silently when the workspace had no row there (it is measured
against the env-default threshold only), so "nothing to suspend" and "could
not suspend" looked identical. It now says which.

Needs a Postgres with the migration chain applied; skips otherwise.
"""
from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.hatchet_workflows import cost_burn_watcher as cbw

PG_DSN = os.environ.get("PG_DSN") or (
    "postgresql://{u}:{p}@{h}:{port}/{db}".format(
        u=os.environ.get("POSTGRES_USER", "georag"),
        p=os.environ.get("POSTGRES_PASSWORD", "georag_dev_password"),
        h=os.environ.get("POSTGRES_DIRECT_HOST", os.environ.get("POSTGRES_HOST", "localhost")),
        port=os.environ.get("POSTGRES_DIRECT_PORT", os.environ.get("POSTGRES_PORT", "5432")),
        db=os.environ.get("POSTGRES_DB", "georag"),
    )
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin():  # noqa: ANN201
    try:
        conn = await asyncpg.connect(PG_DSN, timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Postgres at PG_DSN: {exc}")
    try:
        ok = await conn.fetchval(
            "SELECT to_regclass('usage.workspace_cost_ceilings') IS NOT NULL "
            "AND to_regclass('usage.usage_events') IS NOT NULL "
            "AND (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
        )
        if not ok:
            pytest.skip("needs the migrated schema and a superuser PG_DSN")
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def ws(admin: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """A workspace, and the watcher pointed at this database with the
    env-default threshold pinned to $5/h."""
    monkeypatch.delenv("COST_BURN_THRESHOLD_USD_PER_HOUR", raising=False)
    monkeypatch.setattr(cbw, "_build_dsn", lambda *a, **k: PG_DSN)
    workspace_id = str(uuid.uuid4())
    await admin.execute(
        "INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at) "
        "VALUES ($1::uuid, 'cost-burn test', $2, now(), now())",
        workspace_id, f"cost-burn-{workspace_id[:8]}",
    )
    try:
        yield workspace_id
    finally:
        await admin.execute("DELETE FROM usage.usage_events WHERE workspace_id = $1::uuid", workspace_id)
        await admin.execute("DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", workspace_id)
        async with admin.transaction():
            await admin.execute("SET LOCAL session_replication_role = replica")
            await admin.execute("DELETE FROM audit.audit_ledger WHERE workspace_id = $1::uuid", workspace_id)


async def _spend(admin: asyncpg.Connection, workspace_id: str, usd: float) -> None:
    await admin.execute(
        "INSERT INTO usage.usage_events (workspace_id, agent_name, model_profile, projected_cost_usd) "
        "VALUES ($1::uuid, 'chat', 'test', $2)",
        workspace_id, Decimal(str(usd)),
    )


async def _ceiling(
    admin: asyncpg.Connection, workspace_id: str, *, monthly: float, override: bool = False,
) -> None:
    await admin.execute(
        "INSERT INTO usage.workspace_cost_ceilings "
        "(workspace_id, monthly_ceiling_usd, admin_override_enabled) VALUES ($1::uuid, $2, $3)",
        workspace_id, Decimal(str(monthly)), override,
    )


async def _prior_alert(admin: asyncpg.Connection, workspace_id: str) -> None:
    """An unacknowledged alert from a minute ago: the suppression condition."""
    await admin.execute(
        "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, "
        "target_schema, target_table, target_id, payload) "
        "VALUES ($1::uuid, 'workflow', 'cost.burn.alert', 'usage', 'usage_events', $1::text, '{}'::jsonb)",
        workspace_id,
    )


async def _alerts(admin: asyncpg.Connection, workspace_id: str) -> int:
    return int(await admin.fetchval(
        "SELECT count(*) FROM audit.audit_ledger WHERE workspace_id = $1::uuid "
        "AND action_type = 'cost.burn.alert'", workspace_id,
    ))


async def _suspended_at(admin: asyncpg.Connection, workspace_id: str) -> Any:
    return await admin.fetchval(
        "SELECT suspended_at FROM usage.workspace_cost_ceilings WHERE workspace_id = $1::uuid",
        workspace_id,
    )


class _Tick:
    """One run of the watcher with Redis and Laravel stubbed."""

    def __init__(self) -> None:
        self.redis = AsyncMock()

    async def __call__(self) -> cbw.CostBurnWatcherOutput:
        with (
            patch.object(cbw, "_write_redis_suspension_flag", self.redis),
            patch("app.services.laravel_bridge.post_admin_surface_updated", AsyncMock()),
        ):
            return await cbw.run_watch.aio_mock_run(cbw.CostBurnWatcherInput())


async def test_the_hard_stop_fires_while_the_alert_is_suppressed(
    admin: asyncpg.Connection, ws: str,
) -> None:
    """The reported defect: an earlier, unacknowledged alert must not shield
    a workspace that has since climbed past 2x."""
    await _ceiling(admin, ws, monthly=720.0)         # threshold = $1.00/h, hard stop at $2.00/h
    await _prior_alert(admin, ws)
    await _spend(admin, ws, 3.0)                     # 3x

    tick = _Tick()
    out = await tick()

    assert await _suspended_at(admin, ws) is not None, "the 2x hard stop never ran"
    assert out.workspaces_suspended >= 1
    assert out.alerts_suppressed_idempotent >= 1
    assert await _alerts(admin, ws) == 1, "the suppressed alert must stay suppressed"
    tick.redis.assert_awaited_once_with(ws)


async def test_between_1x_and_2x_an_existing_alert_still_suspends_nothing(
    admin: asyncpg.Connection, ws: str,
) -> None:
    await _ceiling(admin, ws, monthly=720.0)
    await _prior_alert(admin, ws)
    await _spend(admin, ws, 1.5)

    tick = _Tick()
    await tick()

    assert await _suspended_at(admin, ws) is None
    assert await _alerts(admin, ws) == 1
    tick.redis.assert_not_awaited()


async def test_a_first_overrun_past_2x_alerts_and_suspends_in_one_tick(
    admin: asyncpg.Connection, ws: str,
) -> None:
    await _ceiling(admin, ws, monthly=720.0)
    await _spend(admin, ws, 2.5)

    tick = _Tick()
    await tick()

    assert await _alerts(admin, ws) == 1
    assert await _suspended_at(admin, ws) is not None


async def test_the_hard_stop_is_idempotent_across_ticks(admin: asyncpg.Connection, ws: str) -> None:
    await _ceiling(admin, ws, monthly=720.0)
    await _prior_alert(admin, ws)
    await _spend(admin, ws, 3.0)

    await _Tick()()
    first = await _suspended_at(admin, ws)
    tick = _Tick()
    await tick()

    assert await _suspended_at(admin, ws) == first, "a suspended workspace is not re-stamped"
    assert await _alerts(admin, ws) == 1
    tick.redis.assert_not_awaited()


async def test_an_admin_override_still_blocks_the_suspension(admin: asyncpg.Connection, ws: str) -> None:
    await _ceiling(admin, ws, monthly=720.0, override=True)
    await _prior_alert(admin, ws)
    await _spend(admin, ws, 5.0)

    tick = _Tick()
    await tick()

    assert await _suspended_at(admin, ws) is None
    tick.redis.assert_not_awaited()


async def test_a_workspace_with_no_ceiling_row_is_reported_not_silently_skipped(
    admin: asyncpg.Connection, ws: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """No row means there is nothing to suspend; that used to look exactly like
    'already suspended'. Past 2x of the env default ($5/h) it must say so."""
    await _prior_alert(admin, ws)
    await _spend(admin, ws, 12.0)                    # 2.4x the $5/h env default

    tick = _Tick()
    with caplog.at_level(logging.ERROR, logger="georag.hatchet.cost_burn_watcher"):
        out = await tick()

    assert out.hard_stop_unenforceable >= 1
    marker_lines = [
        r.getMessage() for r in caplog.records
        if cbw.HARD_STOP_UNENFORCEABLE_MARKER in r.getMessage() and ws in r.getMessage()
    ]
    assert marker_lines, "the unenforceable hard stop must be logged with its marker"
    assert "NOT applied" in marker_lines[0]
    # ... and nothing was invented: the watcher does not create a ceiling.
    assert await admin.fetchval(
        "SELECT count(*) FROM usage.workspace_cost_ceilings WHERE workspace_id = $1::uuid", ws,
    ) == 0
    tick.redis.assert_not_awaited()


async def test_suspend_workspace_says_which_kind_of_nothing_it_did(
    admin: asyncpg.Connection, ws: str,
) -> None:
    async def call() -> str:
        async with admin.transaction():
            await admin.execute("SELECT set_config('app.workspace_id', $1, true)", ws)
            with patch.object(cbw, "_write_redis_suspension_flag", AsyncMock()):
                return await cbw._suspend_workspace(admin, ws, 9.0, 1.0)

    assert await call() == cbw._NO_CEILING_ROW
    await _ceiling(admin, ws, monthly=720.0)
    assert await call() == cbw._SUSPENDED
    assert await call() == cbw._ALREADY_SUSPENDED
