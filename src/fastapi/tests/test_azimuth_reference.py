"""GIS-12 (audit 2026-09-29): azimuth reference, applied only when declared.

Default unchanged (Kyle): with no recognised reference nothing is applied.
A declared true / magnetic / other-grid reference is converted to the grid
of the collar's own UTM zone, which is the frame the trace is built in.
"""
from __future__ import annotations

import math

import pytest

from app.services.ingest.azimuth_reference import (
    apply,
    azimuth_correction,
    correct_survey_rows,
    true_north_bearing_in_grid,
)


def _corr(ref: str | None, *, decl: float | None = None, project: int | None = None,
          lon: float = -102.1, lat: float = 58.0):
    return azimuth_correction(
        orientation_reference=ref, magnetic_declination=decl,
        project_epsg=project, local_epsg=32613, lon=lon, lat=lat,
    )


@pytest.mark.parametrize("ref", [None, "", "BOH", "TOH", "whatever"])
def test_nothing_declared_means_nothing_applied(ref: str | None) -> None:
    c = _corr(ref, decl=12.0)
    assert c.degrees == 0.0 and c.reference == "none"
    assert apply(45.0, c) == 45.0


def test_convergence_magnitude_matches_the_audit() -> None:
    # Audit: (-102.1, 58) in zone 13 has 2.46 degrees of convergence.
    beta = true_north_bearing_in_grid(32613, -102.1, 58.0)
    assert abs(beta) == pytest.approx(2.46, abs=0.02)
    # East of the central meridian (-105), true north is WEST of grid north.
    assert beta < 0


def test_true_north_reference_is_corrected() -> None:
    c = _corr("true_north")
    assert c.reference == "true"
    assert c.degrees == pytest.approx(true_north_bearing_in_grid(32613, -102.1, 58.0))
    # Due-north true azimuth is ~357.5 in the zone-13 grid there.
    assert apply(0.0, c) == pytest.approx(360.0 + c.degrees)


def test_magnetic_reference_uses_declination_then_convergence() -> None:
    c = _corr("magnetic", decl=8.0)
    beta = true_north_bearing_in_grid(32613, -102.1, 58.0)
    assert c.degrees == pytest.approx(8.0 + beta)


def test_magnetic_without_declination_is_not_applied() -> None:
    c = _corr("magnetic", decl=None)
    assert c.degrees == 0.0 and c.note


def test_grid_of_the_same_zone_is_no_change() -> None:
    assert _corr("grid_north", project=32613).degrees == 0.0
    assert _corr("grid", project=None).degrees == 0.0


def test_grid_of_another_zone_is_converted() -> None:
    # Project in zone 14, collar computed in its own zone 13 (near the
    # boundary): azimuths are zone-14 grid, the trace is zone-13 grid.
    c = _corr("grid", project=32614, lon=-102.1)
    expected = (
        true_north_bearing_in_grid(32613, -102.1, 58.0)
        - true_north_bearing_in_grid(32614, -102.1, 58.0)
    )
    assert c.degrees == pytest.approx(expected)
    assert abs(c.degrees) > 1.0


# ---------------------------------------------------------------------------
# Wired into promote_silver_to_gold._promote_traces (Hatchet import: CI env)
# ---------------------------------------------------------------------------


class _TraceConn:
    def __init__(
        self,
        reference: str | None,
        *,
        surveys: list[dict] | None = None,
        declination: float | None = None,
    ) -> None:
        self.reference = reference
        self.surveys = surveys or []
        self.declination = declination
        self.wkts: list[str] = []
        self.survey_sql: list[str] = []
        self.survey_calls: list[tuple[str, tuple[object, ...]]] = []
        self.upsert_batches: list[list[tuple[object, ...]]] = []

    async def fetchrow(self, sql: str, *args: object) -> dict | None:
        return {"orientation_reference": self.reference,
                "magnetic_declination": self.declination, "crs_epsg": 32613}

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM silver.collars" in sql:
            return [{"collar_id": "c1", "elevation": 500.0, "total_depth": 500.0,
                     "azimuth": 0.0, "dip": -45.0, "lon": -102.1, "lat": 58.0,
                     "existing_hash": None}]
        self.survey_sql.append(sql)
        self.survey_calls.append((sql, args))
        # [] = no surveys: straight-line fallback from the collar. The batched
        # read returns every station of every requested hole, keyed by hole.
        return [{"collar_id": "c1", **row} for row in self.surveys]

    async def executemany(self, sql: str, args_list: list[tuple[object, ...]]) -> None:
        self.upsert_batches.append(list(args_list))
        for args in args_list:
            self.wkts.append(str(args[3]))


async def _promote(conn: _TraceConn):  # type: ignore[no-untyped-def]
    from app.hatchet_workflows import promote_silver_to_gold as m

    out = m.PromoteSilverToGoldOutput()
    await m._promote_traces(conn, workspace_id="w", project_id="p", out=out)  # type: ignore[arg-type]
    return out


def _toe(conn: _TraceConn) -> tuple[float, float]:
    last = conn.wkts[0].split("(")[1].rstrip(")").split(",")[-1].split()
    return float(last[0]), float(last[1])


async def _toe_offset(reference: str | None) -> tuple[float, float]:
    conn = _TraceConn(reference)
    await _promote(conn)
    return _toe(conn)


@pytest.mark.asyncio
async def test_undeclared_reference_builds_the_same_trace_as_before() -> None:
    east, north = await _toe_offset(None)
    assert east == pytest.approx(0.0, abs=1e-6)   # due north in the grid
    assert north > 0


@pytest.mark.asyncio
async def test_true_north_reference_rotates_the_trace_by_the_convergence() -> None:
    east, north = await _toe_offset("true")
    # ~2.46 degrees west of grid north: tan(2.46 deg) = 0.043.
    assert east == pytest.approx(-north * 0.043, rel=0.05)


# ---------------------------------------------------------------------------
# The correction math at real project locations (Kyle, 2026-09-29)
# ---------------------------------------------------------------------------
#
# true_north_bearing_in_grid MEASURES the convergence by projecting a short
# northward step, so it is checked here against the independent closed form
# for Transverse Mercator on the sphere,
#
#     bearing of true north in the grid = -atan(tan(lon - CM) * sin(lat)),
#
# i.e. true north lies WEST of grid north east of the central meridian in the
# northern hemisphere, and the sign flips with either. The ellipsoidal terms
# are far below the 0.01 degree tolerance at these latitudes.

_SITES = [
    # name,                     epsg,  lon,       lat,     central meridian
    ("McArthur River, SK",     32613, -105.05,    57.76,   -105.0),
    ("Apollo-Sitka, AK",       32604, -160.558,   55.192,  -159.0),
    ("Meadowbank, NU",         32615,  -96.0,     65.0,     -93.0),
    ("Labrador Trough, NL",    32619,  -66.8,     54.8,     -69.0),
    ("Kalgoorlie, WA (south)", 32751,  121.47,   -30.75,    123.0),
]


def _closed_form(lon: float, lat: float, cm: float) -> float:
    return -math.degrees(math.atan(math.tan(math.radians(lon - cm)) * math.sin(math.radians(lat))))


@pytest.mark.parametrize(("name", "epsg", "lon", "lat", "cm"), _SITES, ids=[s[0] for s in _SITES])
def test_convergence_matches_the_closed_form(
    name: str, epsg: int, lon: float, lat: float, cm: float,
) -> None:
    measured = true_north_bearing_in_grid(epsg, lon, lat)
    assert measured == pytest.approx(_closed_form(lon, lat, cm), abs=0.01), name


def test_convergence_signs_by_quadrant() -> None:
    # West of CM, northern hemisphere: true north is EAST of grid north.
    assert true_north_bearing_in_grid(32604, -160.558, 55.192) == pytest.approx(1.279, abs=0.005)
    # East of CM, north: WEST.
    assert true_north_bearing_in_grid(32619, -66.8, 54.8) == pytest.approx(-1.798, abs=0.005)
    # West of CM, SOUTHERN hemisphere: sign flips with latitude.
    assert true_north_bearing_in_grid(32751, 121.47, -30.75) == pytest.approx(-0.782, abs=0.005)
    # High latitude, 3 degrees off the CM: the audit's "2.5-2.7 degrees".
    assert true_north_bearing_in_grid(32615, -96.0, 65.0) == pytest.approx(2.719, abs=0.005)


def test_true_north_azimuth_to_grid_at_meadowbank() -> None:
    # A hole surveyed at 045 true, 3 degrees west of the zone-15 CM at 65 N,
    # is 047.72 in the grid the trace is built in.
    c = azimuth_correction(
        orientation_reference="true", magnetic_declination=None,
        project_epsg=32615, local_epsg=32615, lon=-96.0, lat=65.0,
    )
    assert apply(45.0, c) == pytest.approx(47.719, abs=0.005)


def test_east_declination_at_sitka() -> None:
    # Declination east positive. Magnetic 090 with 14.5 E declination is
    # 104.5 true, and 104.5 + 1.279 convergence = 105.78 in zone 4N grid.
    c = azimuth_correction(
        orientation_reference="magnetic", magnetic_declination=14.5,
        project_epsg=32604, local_epsg=32604, lon=-160.558, lat=55.192,
    )
    assert c.reference == "magnetic"
    assert c.degrees == pytest.approx(14.5 + 1.2793, abs=0.001)
    assert apply(90.0, c) == pytest.approx(105.779, abs=0.005)
    # ...and wraps through north rather than past 360.
    assert apply(350.0, c) == pytest.approx(5.779, abs=0.005)


def test_west_declination_in_labrador() -> None:
    # 17 W is -17. Magnetic 010 -> 353 true -> 351.20 in the zone-19 grid.
    c = azimuth_correction(
        orientation_reference="magnetic", magnetic_declination=-17.0,
        project_epsg=32619, local_epsg=32619, lon=-66.8, lat=54.8,
    )
    assert c.degrees == pytest.approx(-17.0 - 1.798, abs=0.001)
    assert apply(10.0, c) == pytest.approx(351.202, abs=0.005)


def test_uncorrected_convergence_moves_the_toe_metres() -> None:
    # What the correction buys: a 500 m hole at 60 degrees from horizontal
    # has 250 m of horizontal reach; 2.72 degrees of uncorrected convergence
    # moves its toe by 250 * 2 * sin(1.36 deg) = ~11.9 m.
    beta = true_north_bearing_in_grid(32615, -96.0, 65.0)
    toe_shift = 250.0 * 2 * math.sin(math.radians(abs(beta) / 2))
    assert toe_shift == pytest.approx(11.87, abs=0.05)


# ---------------------------------------------------------------------------
# Per-file reference: silver.surveys.azimuth_reference wins over the project
# ---------------------------------------------------------------------------

_SITKA = {"project_epsg": 32604, "local_epsg": 32604, "lon": -160.558, "lat": 55.192}


def test_survey_reference_applies_where_the_project_declares_nothing() -> None:
    rows = [
        {"depth": 0.0, "azimuth": 90.0, "dip": -60.0, "azimuth_reference": "true"},
        {"depth": 50.0, "azimuth": 91.0, "dip": -59.0, "azimuth_reference": "true"},
    ]
    res = correct_survey_rows(rows, project_reference="BOH", magnetic_declination=None, **_SITKA)
    assert res.corrected and res.sources == {"survey"}
    assert [r["azimuth"] for r in res.rows] == pytest.approx([91.2793, 92.2793], abs=1e-3)
    # Everything else about the row is untouched.
    assert [r["depth"] for r in res.rows] == [0.0, 50.0]
    assert rows[0]["azimuth"] == 90.0, "the input rows must not be mutated"


def test_survey_reference_overrides_the_project_default() -> None:
    # The project says magnetic; this file says its azimuths are grid. Grid
    # of the project's own zone is no correction at all — the file wins.
    rows = [{"depth": 10.0, "azimuth": 200.0, "dip": -70.0, "azimuth_reference": "grid"}]
    res = correct_survey_rows(rows, project_reference="magnetic", magnetic_declination=14.5, **_SITKA)
    assert res.rows[0]["azimuth"] == 200.0
    assert not res.corrected and res.sources == {"survey"}


def test_stations_without_their_own_reference_fall_back_to_the_project() -> None:
    rows = [
        {"depth": 0.0, "azimuth": 90.0, "dip": -60.0, "azimuth_reference": None},
        {"depth": 50.0, "azimuth": 90.0, "dip": -60.0, "azimuth_reference": "grid"},
        {"depth": 99.0, "azimuth": 90.0, "dip": -60.0},
    ]
    res = correct_survey_rows(rows, project_reference="magnetic", magnetic_declination=14.5, **_SITKA)
    got = [r["azimuth"] for r in res.rows]
    assert got == pytest.approx([105.779, 90.0, 105.779], abs=1e-3)
    assert res.sources == {"survey", "project"}


def test_magnetic_file_without_a_project_declination_is_reported_not_guessed() -> None:
    rows = [{"depth": 0.0, "azimuth": 90.0, "dip": -60.0, "azimuth_reference": "MAG"}]
    res = correct_survey_rows(rows, project_reference=None, magnetic_declination=None, **_SITKA)
    assert res.rows[0]["azimuth"] == 90.0
    assert not res.corrected
    assert res.unapplied_notes == ["magnetic reference declared without a declination; not applied"]


def test_null_azimuth_and_undeclared_rows_pass_through() -> None:
    rows = [
        {"depth": 0.0, "azimuth": None, "dip": -60.0, "azimuth_reference": "true"},
        {"depth": 5.0, "azimuth": 12.0, "dip": -60.0, "azimuth_reference": None},
    ]
    res = correct_survey_rows(rows, project_reference=None, magnetic_declination=None, **_SITKA)
    assert res.rows[0]["azimuth"] is None
    assert res.rows[1]["azimuth"] == 12.0
    assert not res.corrected and not res.unapplied_notes


@pytest.mark.parametrize(
    ("raw", "kind"),
    [("True North", "true"), ("TN", "true"), ("magnetic", "magnetic"),
     ("Grid-North", "grid"), ("grid_north", "grid"), ("BOH", "none"),
     ("TOH", "none"), ("UTM", "none"), (None, "none")],
)
def test_project_reference_vocabulary(raw: str | None, kind: str) -> None:
    c = azimuth_correction(
        orientation_reference=raw, magnetic_declination=10.0,
        project_epsg=None, local_epsg=32613, lon=-102.1, lat=58.0,
    )
    assert c.reference == kind


# ---------------------------------------------------------------------------
# ...and through promote_silver_to_gold._promote_traces (Hatchet import: CI env)
# ---------------------------------------------------------------------------


def _survey(depth: float, az: float, ref: str | None) -> dict:
    return {"depth": depth, "azimuth": az, "dip": -45.0, "azimuth_reference": ref}


@pytest.mark.asyncio
async def test_promotion_reads_the_survey_reference_column() -> None:
    conn = _TraceConn(None, surveys=[_survey(0.0, 0.0, "true"), _survey(500.0, 0.0, "true")])
    out = await _promote(conn)
    assert "azimuth_reference" in conn.survey_sql[0]
    east, north = _toe(conn)
    # Project declares nothing; the FILE says true north, so the trace is
    # rotated by the -2.46 degree convergence at (-102.1, 58) in zone 13.
    assert east == pytest.approx(-north * 0.043, rel=0.05)
    assert out.traces_azimuth_corrected == 1
    assert out.traces_azimuth_reference_unapplied == 0


@pytest.mark.asyncio
async def test_promotion_survey_grid_beats_a_true_project() -> None:
    conn = _TraceConn("true", surveys=[_survey(0.0, 0.0, "grid"), _survey(500.0, 0.0, "grid")])
    out = await _promote(conn)
    east, _ = _toe(conn)
    assert east == pytest.approx(0.0, abs=1e-6)
    assert out.traces_azimuth_corrected == 0


@pytest.mark.asyncio
async def test_promotion_counts_a_magnetic_file_it_cannot_correct() -> None:
    conn = _TraceConn(None, surveys=[_survey(0.0, 0.0, "magnetic"), _survey(500.0, 0.0, "magnetic")])
    out = await _promote(conn)
    east, _ = _toe(conn)
    assert east == pytest.approx(0.0, abs=1e-6), "built uncorrected, not guessed"
    assert out.traces_azimuth_reference_unapplied == 1
    assert out.traces_written == 1


@pytest.mark.asyncio
async def test_promotion_applies_project_declination_to_a_magnetic_file() -> None:
    conn = _TraceConn(None, declination=10.0,
                      surveys=[_survey(0.0, 0.0, "magnetic"), _survey(500.0, 0.0, "magnetic")])
    out = await _promote(conn)
    east, north = _toe(conn)
    beta = true_north_bearing_in_grid(32613, -102.1, 58.0)
    assert math.degrees(math.atan2(east, north)) == pytest.approx(10.0 + beta, abs=0.01)
    assert out.traces_azimuth_corrected == 1
