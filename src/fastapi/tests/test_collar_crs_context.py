"""Collar tools say what frame their coordinates are in (GIS audit 2026-10, finding 9).

``query_spatial_collars`` returned raw easting / northing - in whatever CRS the
source file used, recorded nowhere on the row - with nothing to say so, and its
search centre silently defaulted to EPSG:32613 for a project that declares no
CRS (a search in the wrong zone finds nothing, and nobody is told why). It now
returns ``georef_method``, ``crs_confidence`` and ``spatial_uncertainty_m`` per
collar, a digit-free ``position_caveat`` for the model, a note on what
easting / northing are, and which CRS the centre was read in.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps
from app.agent.hallucination.orchestrator_validators import _collect_evidence
from app.agent.tools import (
    COLLAR_COORDINATE_NOTE,
    CollarRecord,
    SpatialQueryResult,
    position_caveat,
    query_spatial_collars,
)

PROJECT = "762b147e-af53-4593-b569-04ee46f31d97"


# ---------------------------------------------------------------------------
# position_caveat
# ---------------------------------------------------------------------------
class TestPositionCaveat:
    def test_an_assumed_crs_is_called_out(self) -> None:
        assert "ASSUMED" in (position_caveat("assumed", 1.0) or "")

    def test_a_poor_fit_is_called_out_at_the_parsers_own_threshold(self) -> None:
        assert position_caveat("declared", 0.49) is not None
        assert position_caveat("declared", 0.5) is None, "0.5 is not 'low': spatial_parser warns below it"

    def test_both_together(self) -> None:
        text = position_caveat("assumed", 0.2) or ""
        assert "ASSUMED" in text and "poorly" in text

    @pytest.mark.parametrize("method", ["declared", "detected", "manual", "survey", None])
    def test_a_declared_or_unknown_method_is_not_a_caveat(self, method: str | None) -> None:
        assert position_caveat(method, None) is None
        assert position_caveat(method, 0.9) is None

    def test_it_never_contains_a_digit(self) -> None:
        """A confidence or an uncertainty is not evidence a claim can cite."""
        for method in ("assumed", "declared", None):
            for conf in (0.0, 0.2, 0.7, None):
                assert not any(ch.isdigit() for ch in (position_caveat(method, conf) or ""))

    def test_the_coordinate_note_adds_no_numbers_for_the_guard_to_ground_on(self) -> None:
        from app.agent.hallucination.orchestrator_validators import _numbers_in

        assert _numbers_in(COLLAR_COORDINATE_NOTE) == []
        assert "not recorded per collar" in COLLAR_COORDINATE_NOTE
        assert "WGS84" in COLLAR_COORDINATE_NOTE


# ---------------------------------------------------------------------------
# query_spatial_collars
# ---------------------------------------------------------------------------
class _TxnCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


@dataclass
class _Ctx:
    deps: AgentDeps


def _row(**over: object) -> dict:
    base = {
        "collar_id": "c1", "hole_id": "RS-01", "easting": 399183.28, "northing": 6120684.45,
        "elevation": 125.0, "total_depth": 350.0, "hole_type": "Diamond", "azimuth": 270.0,
        "dip": -60.0, "status": "Completed", "drill_date": "2023-06-15",
        "longitude": -160.558, "latitude": 55.192, "total_count": 1,
        "georef_method": "assumed", "crs_confidence": 0.2, "spatial_uncertainty_m": 1000.0,
    }
    base.update(over)
    return base


def _ctx(rows: list[dict], centre_row: dict | None = None):
    captured: dict[str, list] = {"fetch": [], "fetchrow": []}
    conn = AsyncMock()

    async def fetch(sql: str, *args: object) -> list[dict]:
        captured["fetch"].append((sql, args))
        return rows

    async def fetchrow(sql: str, *args: object) -> dict | None:
        captured["fetchrow"].append((sql, args))
        return centre_row

    conn.fetch = fetch
    conn.fetchrow = fetchrow
    conn.transaction = MagicMock(return_value=_TxnCM())
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    deps = AgentDeps(
        pg_pool=pool, qdrant_client=None, neo4j_driver=None,  # type: ignore[arg-type]
        project_id="00000000-0000-0000-0000-0000000000aa",
        embedding_model=None, reranker=None,
    )
    return _Ctx(deps=deps), captured


async def test_each_collar_carries_how_far_to_trust_its_position() -> None:
    ctx, _ = _ctx([_row()])
    result = await query_spatial_collars(ctx, project_id=PROJECT)  # type: ignore[arg-type]

    (collar,) = result.collars
    assert collar.georef_method == "assumed"
    assert collar.crs_confidence == pytest.approx(0.2)
    assert collar.spatial_uncertainty_m == pytest.approx(1000.0)
    assert "ASSUMED" in (collar.position_caveat or "")
    assert result.coordinate_note == COLLAR_COORDINATE_NOTE


async def test_a_row_without_the_new_columns_still_maps() -> None:
    row = _row()
    for key in ("georef_method", "crs_confidence", "spatial_uncertainty_m"):
        del row[key]
    ctx, _ = _ctx([row])
    (collar,) = (await query_spatial_collars(ctx, project_id=PROJECT)).collars  # type: ignore[arg-type]
    assert (collar.georef_method, collar.crs_confidence, collar.spatial_uncertainty_m) == (None, None, None)
    assert collar.position_caveat is None


async def test_the_query_reads_the_columns() -> None:
    ctx, captured = _ctx([_row()])
    await query_spatial_collars(ctx, project_id=PROJECT)  # type: ignore[arg-type]
    sql = captured["fetch"][0][0]
    assert "georef_method, crs_confidence, spatial_uncertainty_m" in sql


async def test_the_new_fields_stay_out_of_repr_so_a_row_is_not_truncated() -> None:
    ctx, _ = _ctx([_row()])
    (collar,) = (await query_spatial_collars(ctx, project_id=PROJECT)).collars  # type: ignore[arg-type]
    text = repr(collar)
    for name in ("georef_method", "crs_confidence", "spatial_uncertainty_m", "position_caveat"):
        assert name not in text
    assert "latitude=55.192" in text


# ---- the search centre ------------------------------------------------------
async def test_no_centre_means_no_centre_crs_and_no_extra_query() -> None:
    ctx, captured = _ctx([_row()])
    result = await query_spatial_collars(ctx, project_id=PROJECT)  # type: ignore[arg-type]
    assert result.centre_crs is None and result.centre_crs_defaulted is False
    assert captured["fetchrow"] == []


async def test_a_lon_lat_centre_is_reported_as_such() -> None:
    ctx, _ = _ctx([_row()], {"srid": 4326, "defaulted": True})
    result = await query_spatial_collars(
        ctx, project_id=PROJECT, center_easting=-160.5, center_northing=55.2, radius_m=500.0,  # type: ignore[arg-type]
    )
    assert result.centre_crs == "EPSG:4326 (longitude/latitude)"
    assert result.centre_crs_defaulted is False, "a lon/lat centre never uses the default"


async def test_the_project_crs_is_reported_when_it_was_used() -> None:
    ctx, _ = _ctx([_row()], {"srid": 26904, "defaulted": False})
    result = await query_spatial_collars(
        ctx, project_id=PROJECT, center_easting=399000.0, center_northing=6120000.0, radius_m=500.0,  # type: ignore[arg-type]
    )
    assert result.centre_crs == "EPSG:26904 (the project's declared CRS)"
    assert result.centre_crs_defaulted is False


async def test_the_32613_default_is_reported_as_a_default() -> None:
    ctx, _ = _ctx([], {"srid": 32613, "defaulted": True})
    result = await query_spatial_collars(
        ctx, project_id=PROJECT, center_easting=399000.0, center_northing=6120000.0, radius_m=500.0,  # type: ignore[arg-type]
    )
    assert result.centre_crs_defaulted is True
    assert "DEFAULT" in (result.centre_crs or "") and "EPSG:32613" in (result.centre_crs or "")
    assert result.count == 0, "an empty result is exactly when the user needs to know"


async def test_the_reported_srid_is_decided_by_the_same_case_as_the_filter() -> None:
    """One expression, used twice: they cannot drift apart."""
    ctx, captured = _ctx([], {"srid": 32613, "defaulted": True})
    await query_spatial_collars(
        ctx, project_id=PROJECT, center_easting=399000.0, center_northing=6120000.0, radius_m=500.0,  # type: ignore[arg-type]
    )
    filter_sql = captured["fetch"][0][0]
    centre_sql, centre_args = captured["fetchrow"][0]
    case = centre_sql.split(" AS srid")[0].removeprefix("SELECT ")
    assert case in filter_sql, "the centre report must use the filter's own CASE"
    assert centre_args == (PROJECT, 399000.0, 6120000.0)
    assert case.startswith("CASE WHEN abs($2::double precision) <= 180")


async def test_a_failure_resolving_the_centre_does_not_lose_the_search() -> None:
    ctx, captured = _ctx([_row()])

    async def boom(sql: str, *args: object):
        raise RuntimeError("catalogue unavailable")

    ctx.deps.pg_pool.acquire.return_value.__aenter__.return_value.fetchrow = boom
    result = await query_spatial_collars(
        ctx, project_id=PROJECT, center_easting=399000.0, center_northing=6120000.0, radius_m=500.0,  # type: ignore[arg-type]
    )
    assert result.count == 1 and result.centre_crs is None
    assert captured["fetch"], "the search ran"


# ---------------------------------------------------------------------------
# What the model reads
# ---------------------------------------------------------------------------
def _collar(**over: object) -> CollarRecord:
    base = {
        "hole_id": "RS-01", "collar_id": "c1", "easting": 399183.28, "northing": 6120684.45,
        "elevation": 125.0, "total_depth": 350.0, "hole_type": "Diamond", "azimuth": 270.0,
        "dip": -60.0, "status": "Completed", "drill_date": None,
        "longitude": -160.558, "latitude": 55.192,
        "georef_method": "assumed", "crs_confidence": 0.2, "spatial_uncertainty_m": 1000.0,
        "position_caveat": position_caveat("assumed", 0.2),
    }
    base.update(over)
    return CollarRecord(**base)  # type: ignore[arg-type]


class TestWhatTheModelReads:
    def test_agentic_render_states_the_frame_the_centre_and_the_caveat(self) -> None:
        from app.agent.agentic_retrieval.nodes import _render_structured_result

        result = SpatialQueryResult(
            collars=[_collar()], count=1, data_source="PostGIS silver.collars", total_count=1,
            centre_crs="EPSG:32613 (the DEFAULT: ...)", centre_crs_defaulted=True,
        )
        text = _render_structured_result(result)
        assert "not recorded per collar" in text
        assert "search centre read as EPSG:32613 (the DEFAULT" in text
        assert "[position: coordinate system was ASSUMED" in text
        # the raw numbers are untouched and the numeric trust fields are not shown to it
        assert "easting=399183.28" in text
        assert "crs_confidence" not in text and "1000.0" not in text

    def test_a_clean_collar_gets_no_position_note(self) -> None:
        from app.agent.agentic_retrieval.nodes import _render_structured_result

        clean = _collar(georef_method="declared", crs_confidence=0.9, position_caveat=None)
        text = _render_structured_result(SpatialQueryResult(collars=[clean], count=1, data_source="x"))
        assert "[position:" not in text

    def test_the_older_prompt_path_says_the_same(self) -> None:
        from app.agent.context_builder import _build_context

        result = SpatialQueryResult(
            collars=[_collar()], count=1, data_source="PostGIS silver.collars",
            centre_crs="EPSG:4326 (longitude/latitude)",
        )
        text = _build_context([("query_spatial_collars", result)])
        assert "not recorded per collar" in text
        assert "search centre read as EPSG:4326" in text
        assert "position: coordinate system was ASSUMED" in text


# ---------------------------------------------------------------------------
# Layer 3: a confidence or an uncertainty is not groundable content
# ---------------------------------------------------------------------------
class TestLayer3TreatsTheNewFieldsAsMetadata:
    def _evidence(self):
        result = SpatialQueryResult(
            collars=[_collar(spatial_uncertainty_m=1234.5, crs_confidence=0.37)],
            count=1, data_source="PostGIS silver.collars", total_count=1,
            centre_crs="EPSG:26904 (the project's declared CRS)",
        )
        return _collect_evidence([("query_spatial_collars", result)])

    def test_uncertainty_and_confidence_do_not_ground_anything(self) -> None:
        ev = self._evidence()
        for value in (1234.5, 0.37):
            assert value not in ev.literal, f"{value} must not be groundable"
            assert value not in ev.derivable

    def test_real_collar_numbers_still_do(self) -> None:
        ev = self._evidence()
        for value in (399183.28, 6120684.45, 125.0, 350.0):
            assert value in ev.literal

    def test_the_epsg_code_in_a_string_is_quotable_but_derives_nothing(self) -> None:
        ev = self._evidence()
        assert 26904.0 in ev.literal
        assert 26904.0 not in ev.derivable

    def test_the_guard_lists_the_uncertainty_key(self) -> None:
        from app.agent.hallucination import orchestrator_validators as v

        assert "spatial_uncertainty_m" in v._NON_CONTENT_KEYS
        assert v._NON_CONTENT_KEY_RE.search("crs_confidence"), "excluded by the existing pattern"


def test_the_record_is_still_a_plain_dataclass_with_defaults() -> None:
    fields = {f.name for f in dataclasses.fields(CollarRecord)}
    assert {"georef_method", "crs_confidence", "spatial_uncertainty_m", "position_caveat"} <= fields
    # Existing 12-field constructors keep working.
    CollarRecord(
        hole_id="h", collar_id="c", easting=1.0, northing=2.0, elevation=3.0, total_depth=None,
        hole_type="d", azimuth=0.0, dip=-90.0, status="s", drill_date=None,
    )
