"""Unit tests for ``query_stereonet`` — ADR-0007 PR-2.

Exercises the FastAPI tool against a mocked asyncpg pool + (real) mplstereonet
render. Asserts:

  - rows from gold.structure_measurements_visual map onto StereonetPoint
  - source_row_id is populated for every point (§04i Layer 5)
  - image_base64 is non-empty and decodes to a valid PNG
  - structure_filter narrows the SQL `ANY()` parameter
  - empty pool returns a graceful empty result + blank-axes PNG
  - downsample caps at _STEREONET_MAX_POINTS deterministically

Run with:
    pytest tests/test_query_stereonet.py -v
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps
from app.agent.tools import (
    _STEREONET_MAX_POINTS,
    StereonetPoint,
    StereonetResult,
    _downsample_stereonet_points,
    _render_stereonet_png,
    query_stereonet,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(rows: list[dict]) -> MagicMock:
    """Build an asyncpg-shaped pool that returns ``rows`` from .fetch()."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_pool


def _make_deps(*, pg_pool: object = None) -> AgentDeps:
    """Build a minimal AgentDeps shell for testing."""
    return AgentDeps(
        pg_pool=pg_pool,  # type: ignore[arg-type]
        qdrant_client=None,  # type: ignore[arg-type]
        neo4j_driver=None,  # type: ignore[arg-type]
        project_id="proj-test-uuid",
        embedding_model=None,
        reranker=None,
    )


def _row(
    *,
    source_row_id: str,
    structure_type: str = "foliation",
    strike: float | None = 45.0,
    dip: float | None = 30.0,
    depth: float | None = 100.0,
) -> dict:
    return {
        "source_row_id":    source_row_id,
        "depth":            depth,
        "structure_type":   structure_type,
        "strike_deg":       strike,
        "dip_deg":          dip,
        "dip_direction_deg": (strike + 90.0) % 360.0 if strike is not None else None,
        "plunge_deg":       None,
        "trend_deg":        None,
        "stereonet_x":      0.123,
        "stereonet_y":      -0.456,
    }


# ---------------------------------------------------------------------------
# query_stereonet
# ---------------------------------------------------------------------------


class TestQueryStereonet:
    @pytest.mark.asyncio
    async def test_happy_path_returns_points_and_png(self) -> None:
        rows = [
            _row(source_row_id="v1", structure_type="foliation", strike=45.0, dip=30.0),
            _row(source_row_id="v2", structure_type="joint",     strike=215.0, dip=72.0),
            _row(source_row_id="v3", structure_type="fault",     strike=180.0, dip=60.0),
        ]
        deps = _make_deps(pg_pool=_make_pool(rows))

        result = await query_stereonet(
            deps=deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="762b147e-af53-4593-b569-04ee46f31d97",
        )

        assert isinstance(result, StereonetResult)
        assert result.count == 3
        assert len(result.points) == 3
        assert result.projection == "Schmidt"
        assert result.data_source.startswith("PostGIS gold.structure_measurements_visual")

        # §04i Layer 5 — every point carries its silver-anchored source_row_id.
        for p in result.points:
            assert isinstance(p, StereonetPoint)
            assert p.source_row_id != ""

        # image_base64 must be non-empty and decode to a valid PNG header.
        assert result.image_base64
        decoded = base64.b64decode(result.image_base64)
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.asyncio
    async def test_structure_filter_applies_to_sql(self) -> None:
        captured: list[tuple] = []

        async def _fake_fetch(sql, *args):
            captured.append((sql, args))
            return [_row(source_row_id="v1", structure_type="foliation")]

        mock_conn = AsyncMock()
        mock_conn.fetch = _fake_fetch
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        deps = _make_deps(pg_pool=mock_pool)

        await query_stereonet(
            deps=deps,
            workspace_id="ws",
            project_id="proj",
            structure_filter="foliation",
        )

        assert captured, "fetch was not invoked"
        sql, args = captured[0]
        assert "structure_type = ANY" in sql
        # Third bind arg is the list form even when caller passes a string.
        assert args[2] == ["foliation"]

    @pytest.mark.asyncio
    async def test_structure_filter_list_form(self) -> None:
        captured: list[tuple] = []

        async def _fake_fetch(sql, *args):
            captured.append((sql, args))
            return []

        mock_conn = AsyncMock()
        mock_conn.fetch = _fake_fetch
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        deps = _make_deps(pg_pool=mock_pool)

        await query_stereonet(
            deps=deps,
            workspace_id="ws",
            project_id="proj",
            structure_filter=["foliation", "joint"],
        )

        sql, args = captured[0]
        assert "structure_type = ANY" in sql
        assert args[2] == ["foliation", "joint"]

    @pytest.mark.asyncio
    async def test_empty_pool_returns_graceful_blank(self) -> None:
        deps = _make_deps(pg_pool=None)
        result = await query_stereonet(
            deps=deps,
            workspace_id="ws",
            project_id="proj",
        )
        assert result.count == 0
        assert result.points == []
        # Even an empty input renders a blank-axes PNG so the card slot
        # is always non-null.
        assert result.image_base64
        assert base64.b64decode(result.image_base64)[:8] == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.asyncio
    async def test_db_exception_returns_empty_not_raise(self) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(side_effect=RuntimeError("boom"))
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        deps = _make_deps(pg_pool=mock_pool)

        result = await query_stereonet(
            deps=deps,
            workspace_id="ws",
            project_id="proj",
        )
        assert result.count == 0

    @pytest.mark.asyncio
    async def test_caps_at_max_points(self) -> None:
        # Build twice the max so we can verify the downsample kicks in.
        many_rows = [
            _row(source_row_id=f"v{i:04d}", strike=float(i % 360), dip=30.0)
            for i in range(_STEREONET_MAX_POINTS * 2)
        ]
        deps = _make_deps(pg_pool=_make_pool(many_rows))
        result = await query_stereonet(
            deps=deps,
            workspace_id="ws",
            project_id="proj",
        )
        assert result.count == _STEREONET_MAX_POINTS


# ---------------------------------------------------------------------------
# Helpers — direct unit tests for the pure functions
# ---------------------------------------------------------------------------


class TestDownsample:
    def test_no_op_below_cap(self) -> None:
        rows = [{"x": i} for i in range(10)]
        assert _downsample_stereonet_points(rows, max_points=20) == rows

    def test_deterministic_stride(self) -> None:
        rows = [{"x": i} for i in range(100)]
        a = _downsample_stereonet_points(rows, max_points=10)
        b = _downsample_stereonet_points(rows, max_points=10)
        assert a == b
        assert len(a) == 10


class TestRenderStereonetPng:
    def test_empty_renders_blank_png(self) -> None:
        b64 = _render_stereonet_png([])
        assert b64
        decoded = base64.b64decode(b64)
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    def test_populated_renders_png(self) -> None:
        pts = [
            StereonetPoint(
                depth=10.0,
                structure_type="foliation",
                strike_deg=45.0,
                dip_deg=30.0,
                dip_direction_deg=135.0,
                plunge_deg=None,
                trend_deg=None,
                stereonet_x=0.1,
                stereonet_y=0.2,
                source_row_id="v1",
            ),
        ]
        b64 = _render_stereonet_png(pts)
        decoded = base64.b64decode(b64)
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"
