"""Tests for `AgentDeps.acquire_scoped` — the project-scoped PG acquire path
that pairs with the GUC-aware RLS policies (DB review item: RLS).

Contract:

  1. With MULTI_TENANT_ENFORCEMENT_ENABLED=False (default), the helper
     opens a transaction but does NOT set the georag.project_id GUC —
     so the RLS policy's `IS NULL` branch admits every row, preserving
     the historical single-tenant behaviour without any tool change.

  2. With MULTI_TENANT_ENFORCEMENT_ENABLED=True, the helper runs
     `SET LOCAL georag.project_id = '<uuid>'` exactly once per acquire
     so the RLS policy's project_id branch matches the caller's project.

  3. A non-UUID project_id MUST be rejected before the SET LOCAL hits
     the wire — defends against an attacker-controlled body project_id
     containing a SQL-injection payload.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps


def _make_pool(execute_mock: AsyncMock):
    """Build a fake asyncpg.Pool where acquire() yields a fake conn."""
    conn = SimpleNamespace(execute=execute_mock)

    # asyncpg's transaction() returns an async context manager.
    class _TxnCM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    conn.transaction = MagicMock(return_value=_TxnCM())

    class _AcquireCM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = SimpleNamespace(acquire=MagicMock(return_value=_AcquireCM()))
    return pool, conn


def _make_deps(project_id: str, pool):
    return AgentDeps(
        pg_pool=pool,
        qdrant_client=None,  # unused
        neo4j_driver=None,  # unused
        project_id=project_id,
    )


@pytest.fixture
def _flag(monkeypatch):
    """Toggle MULTI_TENANT_ENFORCEMENT_ENABLED per-test."""
    from app.config import settings

    original = settings.MULTI_TENANT_ENFORCEMENT_ENABLED

    def _set(v: bool) -> None:
        object.__setattr__(settings, "MULTI_TENANT_ENFORCEMENT_ENABLED", v)

    yield _set
    object.__setattr__(settings, "MULTI_TENANT_ENFORCEMENT_ENABLED", original)


def _calls(execute: AsyncMock) -> list[str]:
    """Return the SQL strings handed to execute(), in order."""
    return [c.args[0] for c in execute.await_args_list]


@pytest.mark.asyncio
async def test_flag_off_sets_only_statement_timeout(_flag):
    """Single-tenant default — statement_timeout fires, GUC does not.

    Even when MULTI_TENANT_ENFORCEMENT_ENABLED is off, every scoped
    acquire MUST set the runaway-query timeout — that protection is
    independent of the multi-tenant rollout.
    """
    _flag(False)
    execute = AsyncMock()
    pool, _conn = _make_pool(execute)
    deps = _make_deps("3a2c6f5e-9d11-4f8a-9b3e-1c2d4e5f6a7b", pool)

    async with deps.acquire_scoped() as conn:
        assert conn is not None

    sqls = _calls(execute)
    assert any("SET LOCAL statement_timeout" in s for s in sqls)
    assert not any("georag.project_id" in s for s in sqls)


@pytest.mark.asyncio
async def test_flag_on_sets_guc_and_timeout(_flag):
    """Multi-tenant on — both SET LOCALs in the same transaction."""
    _flag(True)
    execute = AsyncMock()
    pool, _conn = _make_pool(execute)
    pid = "3a2c6f5e-9d11-4f8a-9b3e-1c2d4e5f6a7b"
    deps = _make_deps(pid, pool)

    async with deps.acquire_scoped() as conn:
        assert conn is not None

    sqls = _calls(execute)
    assert any("SET LOCAL statement_timeout" in s for s in sqls)
    assert any(f"SET LOCAL georag.project_id = '{pid}'" == s for s in sqls)


@pytest.mark.asyncio
async def test_flag_on_rejects_non_uuid_project_id(_flag):
    """Defence: project_id must be a real UUID — no injection escape.

    The statement_timeout SET LOCAL fires BEFORE the project_id check
    because the timeout is unconditional safety; the validation error
    surfaces from the project_id branch, not before.
    """
    _flag(True)
    execute = AsyncMock()
    pool, _conn = _make_pool(execute)
    deps = _make_deps("'; DROP TABLE silver.collars; --", pool)

    with pytest.raises(ValueError, match="non-UUID project_id"):
        async with deps.acquire_scoped():
            pass

    sqls = _calls(execute)
    # statement_timeout is fine to have fired
    assert all("DROP TABLE" not in s for s in sqls), \
        "Injection payload must never reach SQL"


@pytest.mark.asyncio
async def test_flag_on_with_empty_project_id_skips_guc(_flag):
    """Empty project_id → SET LOCAL statement_timeout still fires, GUC skipped.

    RLS policy admits all rows via the IS NULL branch — same as flag-off
    behaviour. Protects against accidental "" or None project_ids erroring.
    """
    _flag(True)
    execute = AsyncMock()
    pool, _conn = _make_pool(execute)
    deps = _make_deps("", pool)

    async with deps.acquire_scoped() as conn:
        assert conn is not None

    sqls = _calls(execute)
    assert any("SET LOCAL statement_timeout" in s for s in sqls)
    assert not any("georag.project_id" in s for s in sqls)
