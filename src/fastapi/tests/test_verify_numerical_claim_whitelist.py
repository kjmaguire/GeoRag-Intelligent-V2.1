"""P0 #2 — column allowlist on verify_numerical_claim.

Regression tests for the SQL-injection fix: the `column` argument used
to be raw-interpolated into the query string. Now it's gated by a
per-table allowlist.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.tools import verify_numerical_claim


def _fake_ctx():
    """Minimal RunContext stand-in — only pg_pool is needed here."""
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn=AsyncMock()))
    deps = SimpleNamespace(pg_pool=pool)
    return SimpleNamespace(deps=deps)


class _AsyncCtxMgr:
    """Hand-rolled async context manager for the pool.acquire() mock."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.fetchrow = AsyncMock(return_value={"total_depth": 510.0})

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_allowed_column_passes():
    """silver.collars.total_depth is whitelisted — call goes through."""
    ctx = _fake_ctx()
    result = await verify_numerical_claim(
        ctx,
        table="silver.collars",
        column="total_depth",
        row_id="00000000-0000-0000-0000-000000000001",
        claimed_value=510.0,
    )
    assert result.verified is True
    assert "BLOCKED" not in result.verification_query


@pytest.mark.asyncio
async def test_sql_injection_in_column_is_blocked():
    """The exact payload the review report flagged — must be rejected."""
    ctx = _fake_ctx()
    result = await verify_numerical_claim(
        ctx,
        table="silver.collars",
        column="total_depth, elevation, (SELECT current_user)",
        row_id="00000000-0000-0000-0000-000000000001",
        claimed_value=1.0,
    )
    assert result.verified is False
    assert "BLOCKED" in result.verification_query
    assert "column" in result.verification_query.lower()


@pytest.mark.asyncio
async def test_wrong_table_blocked():
    ctx = _fake_ctx()
    result = await verify_numerical_claim(
        ctx,
        table="pg_catalog.pg_user",
        column="usename",
        row_id="00000000-0000-0000-0000-000000000001",
        claimed_value=0.0,
    )
    assert result.verified is False
    assert "BLOCKED" in result.verification_query
    assert "table" in result.verification_query.lower()


@pytest.mark.asyncio
async def test_non_numeric_column_blocked():
    """silver.collars.hole_id is a TEXT column — not whitelisted for numeric verification."""
    ctx = _fake_ctx()
    result = await verify_numerical_claim(
        ctx,
        table="silver.collars",
        column="hole_id",
        row_id="00000000-0000-0000-0000-000000000001",
        claimed_value=0.0,
    )
    assert result.verified is False
    assert "BLOCKED" in result.verification_query


@pytest.mark.asyncio
async def test_geometry_column_blocked():
    """Geometry columns can't be compared to a float — not in allowlist."""
    ctx = _fake_ctx()
    result = await verify_numerical_claim(
        ctx,
        table="silver.collars",
        column="geom",
        row_id="00000000-0000-0000-0000-000000000001",
        claimed_value=0.0,
    )
    assert result.verified is False
    assert "BLOCKED" in result.verification_query


@pytest.mark.asyncio
async def test_each_table_has_its_own_column_scope():
    """silver.samples.value is allowed; silver.collars.value is NOT."""
    ctx = _fake_ctx()
    result = await verify_numerical_claim(
        ctx,
        table="silver.collars",
        column="value",   # valid on silver.samples but not on silver.collars
        row_id="00000000-0000-0000-0000-000000000001",
        claimed_value=1.0,
    )
    assert result.verified is False
    assert "BLOCKED" in result.verification_query
