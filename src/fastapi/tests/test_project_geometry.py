"""Unit tests for plan §2g project-bbox geometry supplier."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.project_geometry import get_project_bbox_wkt


# ---------------------------------------------------------------------------
# Mock asyncpg
# ---------------------------------------------------------------------------


class _MockConn:
    def __init__(
        self,
        *,
        bbox_row: dict[str, Any] | None = None,
        envelope_row: dict[str, Any] | None = None,
        bbox_raises: Exception | None = None,
        envelope_raises: Exception | None = None,
    ) -> None:
        self.bbox_row = bbox_row
        self.envelope_row = envelope_row
        self.bbox_raises = bbox_raises
        self.envelope_raises = envelope_raises
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self._fetchrow_pos = 0  # 0 = bbox, 1 = envelope

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        if self._fetchrow_pos == 0:
            self._fetchrow_pos += 1
            if self.bbox_raises:
                raise self.bbox_raises
            return self.bbox_row
        self._fetchrow_pos += 1
        if self.envelope_raises:
            raise self.envelope_raises
        return self.envelope_row

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None
        return _tx()


class _MockPool:
    def __init__(self, conn: _MockConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _ctx():
            yield self.conn
        return _ctx()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_bbox_column_when_populated():
    wkt = "POLYGON((-105 39, -104 39, -104 40, -105 40, -105 39))"
    pool = _MockPool(_MockConn(bbox_row={"wkt": wkt}))
    result = await get_project_bbox_wkt(
        pool, workspace_id="ws-1", project_id="p-1",
    )
    assert result == wkt


@pytest.mark.asyncio
async def test_falls_back_to_envelope_when_bbox_column_missing():
    wkt = "POLYGON((-106 38, -103 38, -103 41, -106 41, -106 38))"
    pool = _MockPool(_MockConn(bbox_row=None, envelope_row={"wkt": wkt}))
    result = await get_project_bbox_wkt(
        pool, workspace_id="ws-1", project_id="p-1",
    )
    assert result == wkt


@pytest.mark.asyncio
async def test_falls_back_to_envelope_when_bbox_column_undefined():
    """A real production gotcha — silver.projects may not have a bbox
    column on every deployment. The supplier catches the exception
    and tries the envelope path."""
    wkt = "POLYGON((-106 38, -103 38, -103 41, -106 41, -106 38))"
    pool = _MockPool(
        _MockConn(
            bbox_raises=RuntimeError("column 'bbox' does not exist"),
            envelope_row={"wkt": wkt},
        ),
    )
    result = await get_project_bbox_wkt(
        pool, workspace_id="ws-1", project_id="p-1",
    )
    assert result == wkt


# ---------------------------------------------------------------------------
# Empty / null paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_when_no_bbox_and_no_collars():
    pool = _MockPool(_MockConn(bbox_row=None, envelope_row=None))
    result = await get_project_bbox_wkt(
        pool, workspace_id="ws-1", project_id="p-1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_envelope_wkt_is_null():
    """ST_Envelope(ST_Collect(...)) returns NULL when no rows match."""
    pool = _MockPool(_MockConn(bbox_row=None, envelope_row={"wkt": None}))
    result = await get_project_bbox_wkt(
        pool, workspace_id="ws-1", project_id="p-1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_empty_project_id_returns_none_without_db_hit():
    conn = _MockConn()
    pool = _MockPool(conn)
    result = await get_project_bbox_wkt(
        pool, workspace_id="ws-1", project_id="",
    )
    assert result is None
    # Pool.acquire() should NOT have been entered.
    assert conn.execute_calls == []
    assert conn.fetchrow_calls == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_workspace_id_raises():
    pool = _MockPool(_MockConn())
    with pytest.raises(ValueError, match="workspace_id is required"):
        await get_project_bbox_wkt(
            pool, workspace_id="", project_id="p-1",
        )


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sets_workspace_guc_inside_transaction():
    conn = _MockConn(bbox_row=None, envelope_row=None)
    pool = _MockPool(conn)
    await get_project_bbox_wkt(
        pool, workspace_id="ws-tenant-7", project_id="p-1",
    )
    assert any(
        "set_config('georag.workspace_id'" in sql
        and args == ("ws-tenant-7",)
        for sql, args in conn.execute_calls
    )


# ---------------------------------------------------------------------------
# Total failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_when_pool_acquire_fails():
    """Connection errors must not propagate — the spatial query
    upstream should skip cleanly."""

    class _BrokenPool:
        def acquire(self):
            @asynccontextmanager
            async def _ctx():
                raise RuntimeError("pool connection failed")
                yield  # unreachable

            return _ctx()

    result = await get_project_bbox_wkt(
        _BrokenPool(), workspace_id="ws-1", project_id="p-1",
    )
    assert result is None
