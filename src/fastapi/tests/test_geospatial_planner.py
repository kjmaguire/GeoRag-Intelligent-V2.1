"""Unit tests for plan §2g geospatial query planner."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.geospatial_planner import (
    SPATIAL_TARGETS,
    SpatialPlan,
    SpatialQuerySpec,
    execute_spatial_query,
    plan_spatial_query,
)

# ---------------------------------------------------------------------------
# SPATIAL_TARGETS table
# ---------------------------------------------------------------------------


def test_spatial_targets_table_covers_four_known_targets():
    """Regression: if a target is removed without updating callers,
    this test breaks loudly."""
    assert set(SPATIAL_TARGETS.keys()) == {
        "silver.collars",
        "silver.spatial_features",
        "public.smdi_deposits",
        "gold.h3_density",
    }


def test_silver_targets_are_workspace_scoped():
    for key in ("silver.collars", "silver.spatial_features", "gold.h3_density"):
        target = SPATIAL_TARGETS[key]
        assert target.workspace_scoped is True, (
            f"{key} must be workspace-scoped (RLS invariant)"
        )


def test_public_smdi_is_intentionally_unscoped():
    target = SPATIAL_TARGETS["public.smdi_deposits"]
    assert target.workspace_scoped is False
    # The geom column on the public reference table is named 'geom'.
    assert target.geom_column == "geom"


@pytest.mark.parametrize("key", list(SPATIAL_TARGETS.keys()))
def test_every_target_specifies_a_crs(key):
    """Every target MUST declare its CRS — silent assumptions are a
    GIS bug pattern we won't repeat."""
    target = SPATIAL_TARGETS[key]
    assert target.crs_epsg > 0


# ---------------------------------------------------------------------------
# Planner — happy path
# ---------------------------------------------------------------------------


def test_intersects_plan_emits_ST_Intersects():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POLYGON((-105 39, -104 39, -104 40, -105 40, -105 39))",
    )
    plan = plan_spatial_query(spec)
    assert isinstance(plan, SpatialPlan)
    assert "ST_Intersects(collar_geom" in plan.sql
    assert "ST_GeomFromText" in plan.sql
    assert plan.params[0] == spec.geometry_wkt
    assert plan.target.table == "silver.collars"


def test_contains_plan_emits_ST_Contains():
    spec = SpatialQuerySpec(
        target="silver.spatial_features",
        operation="contains",
        geometry_wkt="POINT(-104.5 39.5)",
    )
    plan = plan_spatial_query(spec)
    assert "ST_Contains(geom" in plan.sql
    assert "ST_Intersects" not in plan.sql


def test_within_plan_emits_ST_Within():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="within",
        geometry_wkt="POLYGON((-105 39, -104 39, -104 40, -105 40, -105 39))",
    )
    plan = plan_spatial_query(spec)
    assert "ST_Within(collar_geom" in plan.sql


def test_dwithin_plan_emits_ST_DWithin_with_buffer_param():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(-104.5 39.5)",
        buffer_m=1500.0,
    )
    plan = plan_spatial_query(spec)
    assert "ST_DWithin(collar_geom::geography" in plan.sql
    # Buffer becomes the second parameter (after geometry WKT).
    assert plan.params == ("POINT(-104.5 39.5)", 1500.0)
    # The casts are present — geography for accurate distance in metres.
    assert "::geography" in plan.sql
    assert "$2::numeric" in plan.sql


def test_distance_plan_emits_ORDER_BY_ST_Distance():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="distance",
        geometry_wkt="POINT(-104.5 39.5)",
    )
    plan = plan_spatial_query(spec)
    assert "ORDER BY ST_Distance(collar_geom" in plan.sql
    # 'distance' op has no WHERE for the spatial predicate; just the
    # workspace clause when applicable.
    assert "ST_Intersects" not in plan.sql
    assert "ST_DWithin" not in plan.sql


# ---------------------------------------------------------------------------
# Workspace tenancy
# ---------------------------------------------------------------------------


def test_silver_target_pins_workspace_id_predicate():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    plan = plan_spatial_query(spec)
    assert "workspace_id = current_setting('app.workspace_id')::uuid" in plan.sql


def test_public_smdi_does_not_pin_workspace_predicate():
    spec = SpatialQuerySpec(
        target="public.smdi_deposits",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    plan = plan_spatial_query(spec)
    assert "workspace_id" not in plan.sql
    # And the SQL CARRIES an audit comment explaining why.
    assert "intentionally unscoped" in plan.sql


# ---------------------------------------------------------------------------
# CRS mismatch refusal
# ---------------------------------------------------------------------------


def test_crs_mismatch_raises_value_error():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(500000 4400000)",
        crs_epsg=26913,  # UTM zone 13N — target stores EPSG:4326
    )
    with pytest.raises(ValueError, match="CRS mismatch"):
        plan_spatial_query(spec)


# ---------------------------------------------------------------------------
# dwithin requires buffer
# ---------------------------------------------------------------------------


def test_dwithin_without_buffer_raises():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(0 0)",
        buffer_m=None,  # missing
    )
    with pytest.raises(ValueError, match="dwithin.*buffer_m"):
        plan_spatial_query(spec)


def test_dwithin_with_zero_buffer_raises():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(0 0)",
        buffer_m=0.0,
    )
    with pytest.raises(ValueError, match="positive"):
        plan_spatial_query(spec)


def test_dwithin_with_negative_buffer_raises():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(0 0)",
        buffer_m=-100.0,
    )
    with pytest.raises(ValueError, match="positive"):
        plan_spatial_query(spec)


# ---------------------------------------------------------------------------
# Unknown target
# ---------------------------------------------------------------------------


def test_unknown_target_raises_key_error():
    spec = SpatialQuerySpec(
        target="silver.does_not_exist",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    with pytest.raises(KeyError):
        plan_spatial_query(spec)


# ---------------------------------------------------------------------------
# LIMIT handling
# ---------------------------------------------------------------------------


def test_limit_default_is_200():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    plan = plan_spatial_query(spec)
    assert "LIMIT 200" in plan.sql


def test_limit_capped_at_1000():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
        limit=999_999,  # absurdly high
    )
    plan = plan_spatial_query(spec)
    assert "LIMIT 1000" in plan.sql


def test_limit_floored_at_1():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
        limit=0,
    )
    plan = plan_spatial_query(spec)
    assert "LIMIT 1" in plan.sql


# ---------------------------------------------------------------------------
# SELECT column override
# ---------------------------------------------------------------------------


def test_default_select_uses_target_select_columns():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    plan = plan_spatial_query(spec)
    assert "SELECT collar_id, hole_id" in plan.sql


def test_select_override_replaces_default_columns():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
        select_columns=("collar_id",),
    )
    plan = plan_spatial_query(spec)
    assert "SELECT collar_id" in plan.sql
    assert "hole_id" not in plan.sql


# ---------------------------------------------------------------------------
# ORDER BY
# ---------------------------------------------------------------------------


def test_order_by_appended_when_provided():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
        order_by="total_depth_m DESC",
    )
    plan = plan_spatial_query(spec)
    assert "ORDER BY total_depth_m DESC" in plan.sql


def test_distance_op_order_by_distance_AND_secondary_order_by():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="distance",
        geometry_wkt="POINT(0 0)",
        order_by="hole_id ASC",
    )
    plan = plan_spatial_query(spec)
    # distance op injects ST_Distance as primary, then the explicit
    # order_by as secondary.
    assert "ORDER BY ST_Distance" in plan.sql
    assert "hole_id ASC" in plan.sql


# ---------------------------------------------------------------------------
# Signature for trace correlation
# ---------------------------------------------------------------------------


def test_plan_signature_includes_target_and_operation():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    plan = plan_spatial_query(spec)
    assert plan.signature == "silver.collars:intersects"


def test_plan_signature_includes_buffer_for_dwithin():
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(0 0)",
        buffer_m=500.0,
    )
    plan = plan_spatial_query(spec)
    assert "buf=500" in plan.signature


# ---------------------------------------------------------------------------
# Parameter ordering
# ---------------------------------------------------------------------------


def test_params_are_positional_and_in_order():
    """Parameter list must match $1, $2, ... positions in the SQL."""
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(0 0)",
        buffer_m=750.0,
    )
    plan = plan_spatial_query(spec)
    # $1 = geometry, $2 = buffer.
    assert plan.params == ("POINT(0 0)", 750.0)
    assert "$1::text" in plan.sql
    assert "$2::numeric" in plan.sql


# ---------------------------------------------------------------------------
# Executor — async wrapper around mock pool
# ---------------------------------------------------------------------------


class _MockConn:
    """A tiny asyncpg.Connection stand-in. Records calls + returns
    canned rows.

    Exposes ``.execute_calls`` and ``.fetch_calls`` lists so tests
    can assert call shapes."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

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
    """asyncpg.Pool stand-in with ``.acquire()`` context manager."""

    def __init__(self, conn: _MockConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _ctx():
            yield self.conn
        return _ctx()


@pytest.mark.asyncio
async def test_executor_sets_workspace_id_GUC_before_query():
    """The executor must call ``set_config('app.workspace_id', ...)``
    inside the same transaction as the SELECT — RLS depends on it."""
    conn = _MockConn(rows=[{"collar_id": "c1"}])
    pool = _MockPool(conn)

    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    plan = plan_spatial_query(spec)

    rows = await execute_spatial_query(
        pool, plan,
        workspace_id="a0000000-0000-0000-0000-000000000001",
    )
    assert rows == [{"collar_id": "c1"}]
    # set_config was called with the workspace_id.
    assert len(conn.execute_calls) == 1
    set_sql, set_args = conn.execute_calls[0]
    assert "set_config('app.workspace_id'" in set_sql
    assert set_args == ("a0000000-0000-0000-0000-000000000001",)


@pytest.mark.asyncio
async def test_executor_passes_plan_params_to_fetch():
    conn = _MockConn(rows=[])
    pool = _MockPool(conn)
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="dwithin",
        geometry_wkt="POINT(0 0)",
        buffer_m=300.0,
    )
    plan = plan_spatial_query(spec)
    await execute_spatial_query(pool, plan, workspace_id="ws-1")
    fetch_sql, fetch_args = conn.fetch_calls[0]
    assert fetch_sql == plan.sql
    assert fetch_args == plan.params


@pytest.mark.asyncio
async def test_executor_raises_without_workspace_id():
    pool = _MockPool(_MockConn())
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    plan = plan_spatial_query(spec)
    with pytest.raises(ValueError, match="workspace_id is required"):
        await execute_spatial_query(pool, plan, workspace_id="")


@pytest.mark.asyncio
async def test_executor_returns_empty_list_when_no_rows():
    conn = _MockConn(rows=[])
    pool = _MockPool(conn)
    spec = SpatialQuerySpec(
        target="silver.collars",
        operation="intersects",
        geometry_wkt="POINT(0 0)",
    )
    plan = plan_spatial_query(spec)
    rows = await execute_spatial_query(pool, plan, workspace_id="ws-1")
    assert rows == []
