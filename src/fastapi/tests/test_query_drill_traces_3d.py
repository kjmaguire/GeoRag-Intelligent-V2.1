"""Unit tests for ``query_drill_traces_3d`` — ADR-0007 PR-4.

Exercises the async tool against a mocked asyncpg pool. Asserts:

  - rows from silver.collars + silver.drill_traces map onto
    :class:`DrillTraceCollar` with trace_points populated
  - source_row_ids carries collar + interval + structure ids (§04i Layer 5)
  - hole_id filter narrows the SQL bind to ``[ws, project, hole_id]``
  - empty pool / DB errors return a graceful empty result
  - intervals and structures degrade gracefully when their queries fail

Run with::

    pytest tests/test_query_drill_traces_3d.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps
from app.agent.tools import (
    DrillTrace3DResult,
    DrillTraceCollar,
    DrillTraceInterval,
    DrillTraceStructure,
    _parse_linestring_z_points,
    query_drill_traces_3d,
)

WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"
PROJECT_ID = "762b147e-af53-4593-b569-04ee46f31d97"
COLLAR_A = "11111111-1111-1111-1111-111111111111"
COLLAR_B = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(*, pg_pool: object = None) -> AgentDeps:
    """Build a minimal AgentDeps shell for testing."""
    return AgentDeps(
        pg_pool=pg_pool,  # type: ignore[arg-type]
        qdrant_client=None,  # type: ignore[arg-type]
        neo4j_driver=None,  # type: ignore[arg-type]
        project_id=PROJECT_ID,
        embedding_model=None,
        reranker=None,
    )


def _collar_row(
    *,
    collar_id: str = COLLAR_A,
    hole_id: str = "36-1085",
    hole_type: str = "Diamond",
    status: str = "Completed",
    elevation: float = 2000.0,
    total_depth: float = 300.0,
    azimuth: float = 45.0,
    dip: float = -60.0,
    longitude: float = -108.0,
    latitude: float = 56.0,
    trace_wkt: str | None = "LINESTRING Z (-108.0 56.0 2000.0, -108.001 56.001 1740.0)",
) -> dict:
    return {
        "collar_id":   collar_id,
        "hole_id":     hole_id,
        "hole_type":   hole_type,
        "status":      status,
        "elevation":   elevation,
        "total_depth": total_depth,
        "azimuth":     azimuth,
        "dip":         dip,
        "longitude":   longitude,
        "latitude":    latitude,
        "trace_wkt":   trace_wkt,
    }


def _interval_row(*, collar_id: str = COLLAR_A) -> dict:
    return {
        "collar_id":     collar_id,
        "source_row_id": "v-int-1",
        "depth_from":    100.0,
        "depth_to":      105.5,
        "interval_kind": "assay_high_grade",
        "color_hint":    "#a83232",
        "label":         "U3O8 0.34%",
    }


def _structure_row(*, collar_id: str = COLLAR_A) -> dict:
    return {
        "collar_id":      collar_id,
        "source_row_id":  "v-struct-1",
        "depth":          150.0,
        "structure_type": "foliation",
        "strike_deg":     45.0,
        "dip_deg":        30.0,
    }


class _MultiQueryPool:
    """Fake asyncpg pool that returns different rows per SQL substring."""

    def __init__(
        self,
        *,
        collar_rows: list[dict] | None = None,
        interval_rows: list[dict] | None = None,
        structure_rows: list[dict] | None = None,
        raise_on_intervals: bool = False,
        raise_on_structures: bool = False,
    ) -> None:
        self.collar_rows = collar_rows or []
        self.interval_rows = interval_rows or []
        self.structure_rows = structure_rows or []
        self.raise_on_intervals = raise_on_intervals
        self.raise_on_structures = raise_on_structures
        self.captured: list[tuple[str, tuple]] = []

    def _make_conn(self):
        outer = self

        class _Conn:
            async def fetch(self, sql, *args):
                outer.captured.append((sql, args))
                if "FROM silver.collars" in sql:
                    return outer.collar_rows
                if "FROM gold.drillhole_intervals_visual" in sql:
                    if outer.raise_on_intervals:
                        raise RuntimeError("interval fetch boom")
                    return outer.interval_rows
                if "FROM gold.structure_measurements_visual" in sql:
                    if outer.raise_on_structures:
                        raise RuntimeError("structure fetch boom")
                    return outer.structure_rows
                return []

        return _Conn()

    def acquire(self):
        outer = self

        class _Cm:
            async def __aenter__(self_inner):
                return outer._make_conn()

            async def __aexit__(self_inner, *exc):
                return False

        return _Cm()


# ---------------------------------------------------------------------------
# Pure-function helper tests
# ---------------------------------------------------------------------------


class TestParseLinestringZPoints:
    def test_parses_two_point_linestring(self) -> None:
        wkt = "LINESTRING Z (1.0 2.0 3.0, 4.0 5.0 6.0)"
        pts = _parse_linestring_z_points(wkt)
        assert pts == [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]

    def test_empty_string_returns_empty(self) -> None:
        assert _parse_linestring_z_points("") == []

    def test_malformed_returns_empty(self) -> None:
        assert _parse_linestring_z_points("not a linestring") == []


# ---------------------------------------------------------------------------
# query_drill_traces_3d
# ---------------------------------------------------------------------------


class TestQueryDrillTraces3D:
    @pytest.mark.asyncio
    async def test_happy_path_project_wide(self) -> None:
        pool = _MultiQueryPool(
            collar_rows=[
                _collar_row(collar_id=COLLAR_A, hole_id="36-1085"),
                _collar_row(collar_id=COLLAR_B, hole_id="36-1086"),
            ],
            interval_rows=[_interval_row(collar_id=COLLAR_A)],
            structure_rows=[_structure_row(collar_id=COLLAR_A)],
        )
        deps = _make_deps(pg_pool=pool)

        result = await query_drill_traces_3d(
            deps=deps,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
        )

        assert isinstance(result, DrillTrace3DResult)
        assert result.count == 2
        assert result.hole_id_filter is None
        assert len(result.collars) == 2
        assert isinstance(result.collars[0], DrillTraceCollar)
        assert result.collars[0].hole_id == "36-1085"
        # trace_points always non-empty — at minimum the 2-point fallback.
        assert len(result.collars[0].trace_points) >= 2
        # Trace points carry x/y/z/depth_m.
        for tp in result.collars[0].trace_points:
            assert {"x", "y", "z", "depth_m"} <= set(tp.keys())

        # Intervals + structures bound.
        assert len(result.intervals) == 1
        assert isinstance(result.intervals[0], DrillTraceInterval)
        assert result.intervals[0].color_hint.startswith("#")
        assert len(result.structures) == 1
        assert isinstance(result.structures[0], DrillTraceStructure)

        # §04i Layer 5 — every collar / interval / structure id ends up in
        # source_row_ids so the citation guard can verify quoted ids.
        assert COLLAR_A in result.source_row_ids
        assert COLLAR_B in result.source_row_ids
        assert "v-int-1" in result.source_row_ids
        assert "v-struct-1" in result.source_row_ids

    @pytest.mark.asyncio
    async def test_hole_id_filter_narrows_sql_bind(self) -> None:
        pool = _MultiQueryPool(
            collar_rows=[_collar_row(collar_id=COLLAR_A, hole_id="36-1085")],
        )
        deps = _make_deps(pg_pool=pool)

        result = await query_drill_traces_3d(
            deps=deps,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
            hole_id="36-1085",
        )

        assert result.count == 1
        assert result.hole_id_filter == "36-1085"

        # The first captured query is the collar SQL. It must carry
        # (workspace_id, project_id, hole_id) as bind args.
        first_sql, first_args = pool.captured[0]
        assert "FROM silver.collars" in first_sql
        assert first_args == (WORKSPACE_ID, PROJECT_ID, "36-1085")
        # And reference both hole_id and hole_id_canonical columns.
        assert "hole_id" in first_sql
        assert "hole_id_canonical" in first_sql

    @pytest.mark.asyncio
    async def test_empty_pool_returns_empty(self) -> None:
        deps = _make_deps(pg_pool=None)
        result = await query_drill_traces_3d(
            deps=deps,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
        )
        assert result.count == 0
        assert result.collars == []
        assert result.source_row_ids == []

    @pytest.mark.asyncio
    async def test_collar_db_error_returns_empty(self) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(side_effect=RuntimeError("collar boom"))
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        deps = _make_deps(pg_pool=mock_pool)

        result = await query_drill_traces_3d(
            deps=deps,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
        )
        assert result.count == 0

    @pytest.mark.asyncio
    async def test_interval_query_failure_does_not_drop_collars(self) -> None:
        pool = _MultiQueryPool(
            collar_rows=[_collar_row()],
            raise_on_intervals=True,
        )
        deps = _make_deps(pg_pool=pool)
        result = await query_drill_traces_3d(
            deps=deps,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
        )
        # Collars must still be present; intervals degrade to [].
        assert result.count == 1
        assert result.intervals == []

    @pytest.mark.asyncio
    async def test_missing_trace_wkt_falls_back_to_vertical(self) -> None:
        """No row in silver.drill_traces → 2-point vertical placeholder."""
        pool = _MultiQueryPool(
            collar_rows=[_collar_row(trace_wkt=None)],
        )
        deps = _make_deps(pg_pool=pool)
        result = await query_drill_traces_3d(
            deps=deps,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
        )
        assert result.count == 1
        tp = result.collars[0].trace_points
        assert len(tp) == 2
        # Vertical placeholder: same lon/lat at both ends.
        assert tp[0]["x"] == tp[1]["x"]
        assert tp[0]["y"] == tp[1]["y"]
        # Toe sits at elev - total_depth.
        assert tp[1]["z"] == pytest.approx(
            result.collars[0].elevation - result.collars[0].total_depth,
            abs=1e-6,
        )


class TestGisAudit20260929:
    """GIS-8 / GIS-21: geom_4326, no Null Island, dip 0 is horizontal."""

    @pytest.mark.asyncio
    async def test_collar_sql_reads_geom_4326(self) -> None:
        pool = _MultiQueryPool(collar_rows=[_collar_row()])
        await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        first_sql, _ = pool.captured[0]
        assert "ST_X(c.geom_4326)" in first_sql
        assert "ST_Transform(c.geom" not in first_sql

    @pytest.mark.asyncio
    async def test_collar_without_position_is_skipped_not_null_island(self) -> None:
        row = _collar_row()
        row["longitude"] = None
        row["latitude"] = None
        pool = _MultiQueryPool(collar_rows=[row])
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        assert result.collars == []

    @pytest.mark.asyncio
    async def test_horizontal_hole_stays_horizontal(self) -> None:
        pool = _MultiQueryPool(collar_rows=[_collar_row(dip=0.0)])
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        assert result.collars[0].dip == 0.0

    @pytest.mark.asyncio
    async def test_trace_depth_is_measured_not_by_index(self) -> None:
        # Stations at 0, 12, 24 m then 300 m, vertical: index-based labels
        # would read 0 / 100 / 200 / 300.
        wkt = (
            "LINESTRING Z (-108.0 56.0 2000.0, -108.0 56.0 1988.0, "
            "-108.0 56.0 1976.0, -108.0 56.0 1700.0)"
        )
        pool = _MultiQueryPool(collar_rows=[_collar_row(trace_wkt=wkt)])
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        depths = [tp["depth_m"] for tp in result.collars[0].trace_points]
        assert depths == pytest.approx([0.0, 12.0, 24.0, 300.0])


class TestGisAudit202610:
    """Findings 1 and 11: the collar vertex backstop; no invented orientation."""

    # ---- finding 1 -------------------------------------------------------
    @pytest.mark.asyncio
    async def test_a_trace_stored_without_a_collar_vertex_is_anchored_at_the_collar(self) -> None:
        # What the version-2 builder wrote for stations at 30 / 100 / 300 m on
        # a vertical hole from a 2000 m collar: vertex 0 is md 30.
        wkt = "LINESTRING Z (-108.0 56.0 1970.0, -108.0 56.0 1900.0, -108.0 56.0 1700.0)"
        pool = _MultiQueryPool(collar_rows=[_collar_row(trace_wkt=wkt)])
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        tp = result.collars[0].trace_points
        assert [p["depth_m"] for p in tp] == pytest.approx([0.0, 30.0, 100.0, 300.0])
        assert tp[0]["z"] == pytest.approx(2000.0)
        assert not any(p["extrapolated"] for p in tp), "no phantom tail past the last station"

    @pytest.mark.asyncio
    async def test_a_trace_that_starts_at_the_collar_is_untouched(self) -> None:
        wkt = "LINESTRING Z (-108.0 56.0 2000.0, -108.0 56.0 1970.0, -108.0 56.0 1700.0)"
        pool = _MultiQueryPool(collar_rows=[_collar_row(trace_wkt=wkt)])
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        assert [p["depth_m"] for p in result.collars[0].trace_points] == pytest.approx([0.0, 30.0, 300.0])

    # ---- finding 11 ------------------------------------------------------
    @pytest.mark.asyncio
    async def test_the_collar_sql_no_longer_invents_an_orientation(self) -> None:
        pool = _MultiQueryPool(collar_rows=[_collar_row()])
        await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        first_sql, _ = pool.captured[0]
        assert "COALESCE(c.azimuth" not in first_sql
        assert "COALESCE(c.dip" not in first_sql
        assert "c.azimuth::float" in first_sql and "c.dip::float" in first_sql

    @pytest.mark.asyncio
    async def test_a_null_orientation_reaches_the_result_as_none_not_north_and_vertical(self) -> None:
        pool = _MultiQueryPool(collar_rows=[_collar_row(trace_wkt=None, azimuth=None, dip=None)])  # type: ignore[arg-type]
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        collar = result.collars[0]
        assert collar.azimuth is None and collar.dip is None
        assert collar.orientation == "unknown"

    @pytest.mark.asyncio
    async def test_a_recorded_zero_azimuth_is_kept_as_zero(self) -> None:
        pool = _MultiQueryPool(collar_rows=[_collar_row(azimuth=0.0, dip=0.0)])
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        assert result.collars[0].azimuth == 0.0 and result.collars[0].dip == 0.0

    @pytest.mark.asyncio
    async def test_the_vertical_placeholder_is_flagged_not_presented_as_measured(self) -> None:
        pool = _MultiQueryPool(collar_rows=[_collar_row(trace_wkt=None)])
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        collar = result.collars[0]
        collar_pt, toe = collar.trace_points
        assert collar_pt["extrapolated"] is False and "assumed" not in collar_pt
        assert toe["extrapolated"] is True and toe["assumed"] is True
        assert collar.orientation == "unknown"

    @pytest.mark.asyncio
    async def test_a_stored_trace_is_surveyed_and_its_points_are_not_assumed(self) -> None:
        pool = _MultiQueryPool(collar_rows=[_collar_row()])
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        collar = result.collars[0]
        assert collar.orientation == "surveyed"
        assert not any(p.get("assumed") for p in collar.trace_points)

    @pytest.mark.asyncio
    async def test_a_stored_trace_with_no_collar_orientation_is_still_surveyed(self) -> None:
        """Holes with a survey file and no azimuth/dip on the collar row are common."""
        pool = _MultiQueryPool(collar_rows=[_collar_row(azimuth=None, dip=None)])  # type: ignore[arg-type]
        result = await query_drill_traces_3d(
            deps=_make_deps(pg_pool=pool), workspace_id=WORKSPACE_ID, project_id=PROJECT_ID,
        )
        collar = result.collars[0]
        assert (collar.azimuth, collar.dip) == (None, None)
        assert collar.orientation == "surveyed"
