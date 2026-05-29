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
    _downsample_trace_points,
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


class TestDownsampleTracePoints:
    def test_below_cap_is_noop(self) -> None:
        pts = [(float(i), float(i), float(i)) for i in range(10)]
        out = _downsample_trace_points(pts, max_points=20)
        assert out == pts

    def test_preserves_toe(self) -> None:
        pts = [(float(i), 0.0, 0.0) for i in range(100)]
        out = _downsample_trace_points(pts, max_points=5)
        assert len(out) == 5
        # First and last must be preserved.
        assert out[0] == pts[0]
        assert out[-1] == pts[-1]


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
