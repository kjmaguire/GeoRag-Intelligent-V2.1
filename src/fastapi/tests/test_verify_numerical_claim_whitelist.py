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


def _fake_ctx(row: dict | None = None):
    """Minimal RunContext stand-in — only pg_pool is needed here.

    ``row`` overrides the fake fetchrow result (default keeps the
    pre-existing {"total_depth": 510.0} fixture so unrelated tests are
    unaffected).
    """
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn=AsyncMock(), row=row))
    # verify_numerical_claim acquires through AgentDeps.acquire_scoped()
    # (2026-08-22) so RLS applies the fence its conditional WHERE
    # clause cannot build when workspace_id is absent. The stand-in
    # yields the same connection; what is being tested here is the
    # column allowlist, not the transaction.
    deps = SimpleNamespace(
        pg_pool=pool,
        acquire_scoped=lambda: _AsyncCtxMgr(conn=AsyncMock(), row=row),
    )
    return SimpleNamespace(deps=deps)


class _AsyncCtxMgr:
    """Hand-rolled async context manager for the pool.acquire() mock."""

    def __init__(self, conn, row: dict | None = None):
        self.conn = conn
        self.conn.fetchrow = AsyncMock(return_value=row or {"total_depth": 510.0})

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


@pytest.mark.asyncio
async def test_reports_page_count_allowed():
    """silver.reports.page_count is whitelisted (2026-08-15 follow-up) —
    call goes through instead of the old fail-closed BLOCKED response.
    """
    ctx = _fake_ctx(row={"page_count": 42.0})
    result = await verify_numerical_claim(
        ctx,
        table="silver.reports",
        column="page_count",
        row_id="00000000-0000-0000-0000-000000000001",
        claimed_value=42.0,
    )
    assert result.verified is True
    assert "BLOCKED" not in result.verification_query


@pytest.mark.asyncio
async def test_reports_version_number_still_blocked():
    """version_number lives on silver.document_versions, not silver.reports
    — must stay blocked rather than silently resolving to the wrong table.
    """
    ctx = _fake_ctx()
    result = await verify_numerical_claim(
        ctx,
        table="silver.reports",
        column="version_number",
        row_id="00000000-0000-0000-0000-000000000001",
        claimed_value=1.0,
    )
    assert result.verified is False
    assert "BLOCKED" in result.verification_query
