"""Unit tests for the wired geospatial tool (plan §2g)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.tools_geospatial import (
    SpatialGeometryResult,
    SpatialIntentHints,
    extract_spatial_intent_keywords,
    query_spatial_geometry,
)


# ---------------------------------------------------------------------------
# Mock asyncpg (mirrors test_geospatial_planner.py)
# ---------------------------------------------------------------------------


class _MockConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return self.rows

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


class _FakeDeps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool


# ---------------------------------------------------------------------------
# extract_spatial_intent_keywords
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected_op,expected_buffer", [
    ("collars within 500 m of the corridor", "dwithin", 500.0),
    ("collars within 2 km of the corridor", "dwithin", 2000.0),
    ("show me collars NEAR the high-grade zone", "dwithin", None),
    ("collars that intersect the zone polygon", "intersects", None),
    ("which collars CONTAIN the assay anomaly?", "contains", None),
    ("which spatial features are WITHIN the corridor outline?", "within", None),
    ("sort by distance to the centroid", "distance", None),
    ("what is the geology of the corridor?", None, None),
])
def test_extract_spatial_intent_keywords_operation_and_buffer(
    text, expected_op, expected_buffer,
):
    hints = extract_spatial_intent_keywords(text)
    assert hints.operation == expected_op
    assert hints.buffer_m == expected_buffer


@pytest.mark.parametrize("text,expected_target", [
    ("drillholes within 500 m of the zone", "silver.collars"),
    ("SMDI occurrences near the property", "public.smdi_deposits"),
    ("mineral occurrences in the corridor", "public.smdi_deposits"),
    ("spatial features in the corridor outline", "silver.spatial_features"),
    ("h3 density grid for the area", "gold.h3_density"),
    ("just a random question", None),
])
def test_extract_spatial_intent_keywords_target(text, expected_target):
    hints = extract_spatial_intent_keywords(text)
    assert hints.target == expected_target


def test_extract_spatial_intent_keywords_empty_returns_all_none():
    hints = extract_spatial_intent_keywords("")
    assert hints.operation is None
    assert hints.buffer_m is None
    assert hints.target is None


# ---------------------------------------------------------------------------
# query_spatial_geometry — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_spatial_geometry_intersects_returns_rows():
    rows = [{"collar_id": "c1", "hole_id": "PLS-22-08"}]
    pool = _MockPool(_MockConn(rows=rows))
    deps = _FakeDeps(pool)

    result = await query_spatial_geometry(
        deps,
        workspace_id="ws-1",
        project_id="p1",
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    )
    assert isinstance(result, SpatialGeometryResult)
    assert result.target == "silver.collars"
    assert result.operation == "intersects"
    assert result.count == 1
    assert result.rows == rows
    assert "intersects" in result.plan_signature


@pytest.mark.asyncio
async def test_query_spatial_geometry_dwithin_with_buffer():
    pool = _MockPool(_MockConn(rows=[]))
    deps = _FakeDeps(pool)
    result = await query_spatial_geometry(
        deps,
        workspace_id="ws-1",
        project_id="p1",
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(0 0)",
        buffer_m=1000.0,
    )
    assert result is not None
    assert result.operation == "dwithin"
    assert result.buffer_m == 1000.0


@pytest.mark.asyncio
async def test_query_spatial_geometry_dwithin_defaults_buffer_when_missing():
    """dwithin without an explicit buffer_m should default to 500 m
    rather than failing — buffer is a tunable, not a hard requirement
    when keyword extraction surfaced the operation."""
    pool = _MockPool(_MockConn(rows=[]))
    deps = _FakeDeps(pool)
    result = await query_spatial_geometry(
        deps,
        workspace_id="ws-1",
        project_id="p1",
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(0 0)",
        # no buffer_m
    )
    assert result is not None
    assert result.buffer_m == 500.0


# ---------------------------------------------------------------------------
# Keyword extraction fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_text_keywords_fill_missing_target_and_op():
    pool = _MockPool(_MockConn(rows=[]))
    deps = _FakeDeps(pool)
    result = await query_spatial_geometry(
        deps,
        workspace_id="ws-1",
        project_id="p1",
        geometry_wkt="POINT(0 0)",
        query_text="show collars within 750 m of the corridor",
        # no explicit target / operation / buffer_m
    )
    assert result is not None
    assert result.target == "silver.collars"
    assert result.operation == "dwithin"
    assert result.buffer_m == 750.0


@pytest.mark.asyncio
async def test_query_text_keywords_dont_override_caller_supplied():
    """When the caller passes a value, the keyword extractor MUST NOT
    overwrite it."""
    pool = _MockPool(_MockConn(rows=[]))
    deps = _FakeDeps(pool)
    result = await query_spatial_geometry(
        deps,
        workspace_id="ws-1",
        project_id="p1",
        target="silver.spatial_features",  # caller-supplied
        operation="intersects",             # caller-supplied
        geometry_wkt="POINT(0 0)",
        query_text="collars within 500 m of the corridor",  # would suggest collars+dwithin
    )
    assert result is not None
    assert result.target == "silver.spatial_features"
    assert result.operation == "intersects"


# ---------------------------------------------------------------------------
# Validation + early-exit paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_workspace_id_raises():
    deps = _FakeDeps(_MockPool(_MockConn()))
    with pytest.raises(ValueError, match="workspace_id is required"):
        await query_spatial_geometry(
            deps,
            workspace_id="",
            project_id="p1",
            target="silver.collars",
            operation="intersects",
            geometry_wkt="POINT(0 0)",
        )


@pytest.mark.asyncio
async def test_missing_geometry_wkt_returns_none():
    """No geometry → no SQL — the tool does NOT invent geometries."""
    deps = _FakeDeps(_MockPool(_MockConn()))
    result = await query_spatial_geometry(
        deps,
        workspace_id="ws-1",
        project_id="p1",
        target="silver.collars",
        operation="intersects",
        geometry_wkt=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_missing_pool_returns_none():
    class _NoPoolDeps:
        pg_pool = None

    result = await query_spatial_geometry(
        _NoPoolDeps(),
        workspace_id="ws-1",
        project_id="p1",
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    assert result is None


@pytest.mark.asyncio
async def test_unknown_target_returns_none_after_logging():
    """An unknown target (caller typo or hint mismatch) skips cleanly."""
    deps = _FakeDeps(_MockPool(_MockConn()))
    result = await query_spatial_geometry(
        deps,
        workspace_id="ws-1",
        project_id="p1",
        target="silver.does_not_exist",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    assert result is None


@pytest.mark.asyncio
async def test_planner_validation_failure_returns_none():
    """Planner raises on CRS mismatch — the tool catches it and
    returns None rather than propagating the exception."""
    deps = _FakeDeps(_MockPool(_MockConn()))
    result = await query_spatial_geometry(
        deps,
        workspace_id="ws-1",
        project_id="p1",
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(500000 4400000)",
        crs_epsg=26913,  # mismatch with silver.collars default 4326
    )
    assert result is None


# ---------------------------------------------------------------------------
# Workspace tenancy check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_sets_workspace_id_guc_inside_transaction():
    conn = _MockConn(rows=[])
    pool = _MockPool(conn)
    deps = _FakeDeps(pool)
    await query_spatial_geometry(
        deps,
        workspace_id="ws-tenant-9",
        project_id="p1",
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    # First execute call inside the transaction is set_config.
    assert any(
        "set_config('app.workspace_id'" in sql and args == ("ws-tenant-9",)
        for sql, args in conn.execute_calls
    )
