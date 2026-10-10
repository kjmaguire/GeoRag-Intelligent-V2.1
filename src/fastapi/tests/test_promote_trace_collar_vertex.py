"""The stored drill trace starts AT THE COLLAR (GIS audit 2026-10, finding 1).

``minimum_curvature`` returns one point per survey station. The collar is a
station only when the survey has a row at depth 0, so a survey whose first
reading is at 30 m produced a LINESTRING whose vertex 0 was measured depth
30. Every reader (``query_drill_traces_3d`` -> ``trace_points_with_depth``)
treats vertex 0 as md 0, so each interval was drawn 30 m uphole of where it
is, and a 30 m phantom tail was appended at the toe to reach total depth.
"""
from __future__ import annotations

import math

import pytest

from app.agent.tools import _parse_linestring_z_points
from app.agent.trace_depth import trace_points_with_depth
from app.hatchet_workflows import promote_silver_to_gold as m

_LON, _LAT, _ELEV = -106.0, 58.0, 100.0


class _Conn:
    """One collar; its survey rows are whatever the test hands in."""

    def __init__(
        self,
        survey: list[tuple[float, float, float]],
        *,
        existing_hash: str | None = None,
        total_depth: float | None = 200.0,
    ) -> None:
        self.survey = survey
        self.existing_hash = existing_hash
        self.total_depth = total_depth
        self.batches: list[list[tuple[object, ...]]] = []

    async def fetchrow(self, sql: str, *args: object) -> dict:
        return {"orientation_reference": None, "magnetic_declination": None, "crs_epsg": None}

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM silver.collars" in sql:
            return [{
                "collar_id": "c0", "elevation": _ELEV, "total_depth": self.total_depth,
                "azimuth": 90.0, "dip": -60.0, "lon": _LON, "lat": _LAT,
                "existing_hash": self.existing_hash,
            }]
        return [
            {"collar_id": "c0", "depth": d, "azimuth": az, "dip": dip, "azimuth_reference": None}
            for d, az, dip in self.survey
        ]

    async def execute(self, sql: str, *args: object) -> str:
        return "OK"

    async def executemany(self, sql: str, args_list: list[tuple[object, ...]]) -> None:
        self.batches.append(list(args_list))


async def _promote(conn: _Conn) -> tuple[list[tuple[float, float, float]], m.PromoteSilverToGoldOutput]:
    out = m.PromoteSilverToGoldOutput()
    await m._promote_traces(conn, workspace_id="w", project_id="p", out=out)  # type: ignore[arg-type]
    if not conn.batches:
        return [], out
    (args,) = conn.batches[0]
    return _parse_linestring_z_points(str(args[3])), out


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------
async def test_a_first_reading_below_the_collar_still_starts_the_line_at_the_collar() -> None:
    pts, out = await _promote(_Conn([(30.0, 90.0, -60.0), (100.0, 90.0, -60.0), (200.0, 90.0, -60.0)]))

    assert out.traces_written == 1
    # collar + the three stations; the offsets are about (0, 0) and Z is absolute.
    assert len(pts) == 4
    east0, north0, z0 = pts[0]
    assert (east0, north0, z0) == pytest.approx((0.0, 0.0, _ELEV))
    # ... and vertex 1 is md 30 along the first station's attitude: the tangent
    # leg the interpolator always assumed.
    east1, north1, z1 = pts[1]
    assert east1 == pytest.approx(30.0 * math.cos(math.radians(60.0)), abs=1e-6)
    assert north1 == pytest.approx(0.0, abs=1e-6)
    assert z1 == pytest.approx(_ELEV - 30.0 * math.sin(math.radians(60.0)), abs=1e-6)


async def test_a_survey_that_already_has_a_collar_row_gets_no_extra_vertex() -> None:
    pts, _ = await _promote(_Conn([(0.0, 90.0, -60.0), (100.0, 90.0, -60.0), (200.0, 90.0, -60.0)]))
    assert len(pts) == 3
    assert pts[0] == pytest.approx((0.0, 0.0, _ELEV))


async def test_the_added_vertex_does_not_move_any_other_vertex() -> None:
    survey = [(30.0, 90.0, -60.0), (100.0, 95.0, -62.0), (200.0, 100.0, -65.0)]
    with_collar, _ = await _promote(_Conn(survey))
    # The same hole with the collar row written out by hand.
    explicit, _ = await _promote(_Conn([(0.0, 90.0, -60.0), *survey]))
    assert with_collar == pytest.approx(explicit)


async def test_the_new_line_gives_the_reader_true_depths_and_no_phantom_tail() -> None:
    """End to end: the writer's line through the reader's depth assignment."""
    pts, _ = await _promote(_Conn([(30.0, 0.0, -90.0), (100.0, 0.0, -90.0), (200.0, 0.0, -90.0)]))
    lonlat = [(_LON, _LAT, z) for _, _, z in pts]  # a vertical hole: no plan offset

    out = trace_points_with_depth(lonlat, 200.0, max_points=50)

    assert [p["depth_m"] for p in out] == pytest.approx([0.0, 30.0, 100.0, 200.0], abs=1e-6)
    assert not any(p["extrapolated"] for p in out), "no tail is invented when the survey reaches TD"


async def test_the_old_line_was_wrong_in_exactly_this_way() -> None:
    """Pins the defect: without the collar vertex the reader shifts and invents."""
    old = [(_LON, _LAT, _ELEV - d) for d in (30.0, 100.0, 200.0)]  # what v2 wrote
    out = trace_points_with_depth(old, 200.0, max_points=50)
    assert out[0]["depth_m"] == 0.0 and out[0]["z"] == pytest.approx(_ELEV - 30.0)
    assert out[-1]["extrapolated"] is True


async def test_dogleg_is_not_changed_by_the_collar_vertex() -> None:
    survey = [(30.0, 90.0, -60.0), (100.0, 95.0, -62.0)]
    conn_a, conn_b = _Conn(survey), _Conn([(0.0, 90.0, -60.0), *survey])
    await _promote(conn_a)
    await _promote(conn_b)
    # dogleg_max_deg is the 9th upsert argument (index 8).
    assert conn_a.batches[0][0][8] == pytest.approx(conn_b.batches[0][0][8])


# ---------------------------------------------------------------------------
# Rebuilding the traces already stored
# ---------------------------------------------------------------------------
def test_the_builder_version_moved_past_the_collarless_builder() -> None:
    assert m._TRACE_BUILDER_VERSION >= 3


async def test_a_trace_stored_by_the_collarless_builder_is_rebuilt_not_skipped() -> None:
    survey = [(30.0, 90.0, -60.0), (100.0, 90.0, -60.0)]
    origin = (_LON, _LAT, _ELEV)
    old_stations = [(30.0, 90.0, -60.0), (100.0, 90.0, -60.0)]

    # The digest the version-2 builder stored for this very hole.
    original = m._TRACE_BUILDER_VERSION
    try:
        m._TRACE_BUILDER_VERSION = 2
        stale = m._survey_hash(old_stations, origin=origin)
    finally:
        m._TRACE_BUILDER_VERSION = original

    pts, out = await _promote(_Conn(survey, existing_hash=stale))

    assert out.traces_unchanged == 0
    assert out.traces_written == 1
    assert len(pts) == 3 and pts[0] == pytest.approx((0.0, 0.0, _ELEV))


async def test_an_up_to_date_trace_is_still_skipped() -> None:
    """Idempotency survives: a nightly re-run writes nothing."""
    survey = [(30.0, 90.0, -60.0), (100.0, 90.0, -60.0)]
    conn = _Conn(survey)
    await _promote(conn)
    current = conn.batches[0][0][7]

    again = _Conn(survey, existing_hash=str(current))
    pts, out = await _promote(again)
    assert pts == [] and out.traces_unchanged == 1 and out.traces_written == 0


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stations,expected_len",
    [
        ([], 0),
        ([(0.0, 10.0, -50.0), (30.0, 10.0, -50.0)], 2),
        ([(30.0, 10.0, -50.0), (60.0, 12.0, -49.0)], 3),
    ],
)
def test_with_collar_station_only_adds_when_the_first_reading_is_below_the_collar(
    stations: list[tuple[float, float, float]], expected_len: int,
) -> None:
    out = m._with_collar_station(stations)
    assert len(out) == expected_len
    if expected_len == 3:
        assert out[0] == (0.0, 10.0, -50.0), "the first station's own attitude, at md 0"
