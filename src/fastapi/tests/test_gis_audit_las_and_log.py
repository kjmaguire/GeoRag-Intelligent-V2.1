"""GIS audit 2026-09-29 — LAS depth units, LAS plausibility, Cameco .log CRS.

* GIS-4 / ING-8: the LAS depth unit (DEPT / STRT / STOP) is read, not
  ignored. Feet become metres on both LAS paths; a file declaring no unit is
  read as metres with a loud ``las_depth_unit_assumed`` (never silently feet).
* GIS-15: a header-placed collar far from the project (positive-west
  longitude) warns.
* GIS-19: EPSG:3736 (NAD83 / Wyoming East, US survey feet) is accepted for
  the .log format, and used without the ftUS -> m conversion 32155 needs.
* GIS-6: every path stores the SOURCE easting/northing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lasio")

from georag_geoparsers.las_parser import (  # noqa: E402
    FEET_TO_METRES,
    parse_las_file,
    unit_to_metres_factor,
)

from app.services.ingest import cameco_log_ingester as cli  # noqa: E402
from app.services.ingest.collar_crs import to_lonlat  # noqa: E402
from app.services.ingest.las_ingester import ingest_las_file  # noqa: E402
from tests.test_las_collar_location import _PJ, _WS, _Conn, _insert  # noqa: E402


def _las(path: Path, *, unit: str, stop: str = "100.0", extra_well: str = "") -> Path:
    u = f".{unit}" if unit else "."
    path.write_text(
        "~VERSION INFORMATION\n"
        " VERS.                 2.0 : CWLS LOG ASCII STANDARD - VERSION 2.0\n"
        " WRAP.                  NO : ONE LINE PER DEPTH STEP\n"
        "~WELL INFORMATION\n"
        f"STRT {u}          0.0 : START DEPTH\n"
        f"STOP {u}   {stop} : STOP DEPTH\n"
        f"STEP {u}          0.5 : STEP\n"
        "NULL .       -999.25 : NULL VALUE\n"
        "COMP .  ACME : COMPANY\n"
        "WELL .  W-1 : WELL\n"
        f"{extra_well}"
        "~CURVE INFORMATION\n"
        f"DEPT {u}  : DEPTH\n"
        "GAMMA.API : GAMMA RAY\n"
        "~ASCII\n"
        "0.0   12.0\n"
        "0.5   14.0\n"
        "1.0   15.0\n",
        encoding="ascii",
    )
    return path


# ---------------------------------------------------------------------------
# GIS-4 — las_parser (the ingest_well_logs path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "factor"), [
    ("F", FEET_TO_METRES), ("FT", FEET_TO_METRES), ("ft", FEET_TO_METRES),
    ("M", 1.0), ("m", 1.0), ("", None), ("S", None),
])
def test_unit_factor(raw: str, factor: float | None) -> None:
    assert unit_to_metres_factor(raw) == factor


def test_feet_las_is_parsed_to_metres(tmp_path: Path) -> None:
    result = parse_las_file(str(_las(tmp_path / "a.las", unit="F")))
    curve = result.curves[0]
    assert curve.depths == pytest.approx([0.0, 0.5 * FEET_TO_METRES, 1.0 * FEET_TO_METRES])
    assert curve.max_depth == pytest.approx(FEET_TO_METRES)
    assert curve.step == pytest.approx(0.5 * FEET_TO_METRES)
    assert result.depth_unit_source == "ft" and not result.depth_unit_assumed
    assert result.warnings == []


def test_metric_las_is_untouched(tmp_path: Path) -> None:
    result = parse_las_file(str(_las(tmp_path / "a.las", unit="M")))
    assert result.curves[0].depths == [0.0, 0.5, 1.0]
    assert result.depth_unit_source == "m"


def test_unitless_las_is_metres_with_a_loud_warning(tmp_path: Path) -> None:
    result = parse_las_file(str(_las(tmp_path / "a.las", unit="")))
    assert result.curves[0].depths == [0.0, 0.5, 1.0]
    assert result.depth_unit_assumed
    assert [w["code"] for w in result.warnings] == ["las_depth_unit_assumed"]
    assert "3.28" in result.warnings[0]["detail"]


# ---------------------------------------------------------------------------
# GIS-4 — las_ingester (the archive / cluster path)
# ---------------------------------------------------------------------------

_LATLON = "LATI .DEG   42.06 : LATITUDE\nLONG .DEG -105.35 : LONGITUDE\nGDAT .   NAD83 : DATUM\n"


class _CurveConn(_Conn):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.curve_args: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO silver.well_log_curves" in sql:
            assert "depth_unit" in sql
            self.curve_args.append(args)
        return await super().execute(sql, *args)


@pytest.mark.asyncio
async def test_feet_stop_becomes_metres_total_depth(tmp_path: Path) -> None:
    conn = _CurveConn()
    result = await ingest_las_file(
        conn, str(_las(tmp_path / "w.las", unit="F", stop="1257.5", extra_well=_LATLON)),
        workspace_id=_WS, project_id_override=_PJ,
    )
    assert not result.skipped
    assert _insert(conn)["total_depth"] == pytest.approx(1257.5 * FEET_TO_METRES)
    depths = conn.curve_args[0][11]
    assert depths[-1] == pytest.approx(1.0 * FEET_TO_METRES)


@pytest.mark.asyncio
async def test_metric_stop_is_unchanged(tmp_path: Path) -> None:
    conn = _CurveConn()
    await ingest_las_file(
        conn, str(_las(tmp_path / "w.las", unit="M", stop="383.3", extra_well=_LATLON)),
        workspace_id=_WS, project_id_override=_PJ,
    )
    assert _insert(conn)["total_depth"] == pytest.approx(383.3)


@pytest.mark.asyncio
async def test_unitless_las_warns_and_reads_metres(tmp_path: Path) -> None:
    conn = _CurveConn()
    result = await ingest_las_file(
        conn, str(_las(tmp_path / "w.las", unit="", stop="383.3", extra_well=_LATLON)),
        workspace_id=_WS, project_id_override=_PJ,
    )
    assert _insert(conn)["total_depth"] == pytest.approx(383.3)
    assert "las_depth_unit_assumed" in [w["code"] for w in result.warnings]


# ---------------------------------------------------------------------------
# GIS-15 — plausibility of a header position
# ---------------------------------------------------------------------------


class _ReferenceConn(_CurveConn):
    """A project whose other collars sit around (-105.35, 42.06)."""

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "percentile_cont" in sql:
            return {"lon": -105.35, "lat": 42.06, "n": 12}
        if "geom_boundary" in sql:
            return None
        return await super().fetchrow(sql, *args)


@pytest.mark.asyncio
async def test_positive_west_longitude_warns(tmp_path: Path) -> None:
    conn = _ReferenceConn()
    header = "LATI .DEG   42.06 : LATITUDE\nLONG .DEG 105.35 : LONGITUDE\nGDAT .   NAD83 : DATUM\n"
    result = await ingest_las_file(
        conn, str(_las(tmp_path / "w.las", unit="F", extra_well=header)),
        workspace_id=_WS, project_id_override=_PJ,
    )
    far = [w for w in result.warnings if w["code"] == "collar_far_from_project"]
    assert far and "opposite hemisphere" in far[0]["detail"]
    assert not result.skipped   # warn, never refuse


@pytest.mark.asyncio
async def test_plausible_header_position_is_quiet(tmp_path: Path) -> None:
    conn = _ReferenceConn()
    result = await ingest_las_file(
        conn, str(_las(tmp_path / "w.las", unit="F", extra_well=_LATLON)),
        workspace_id=_WS, project_id_override=_PJ,
    )
    assert result.warnings == []


# ---------------------------------------------------------------------------
# GIS-19 / GIS-6 — Cameco .log
# ---------------------------------------------------------------------------

_LOG_NAME = "36-1042_08-13-12_10-08_9057C_.10_0.70_1257.50_ORE.log"
_LOG_BYTES = b"CAMECO RESOURCES" + b"\x00" * 20 + b"SHIRLEY BASIN" + b"\x00" * 20 + b"E=791126 N=617244"


class _LogConn:
    def __init__(self) -> None:
        self.args: tuple[Any, ...] | None = None
        self.sql = ""

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sql, self.args = sql, args
        return {"collar_id": "d3000000-0000-0000-0000-0000000000c0"}


def _parsed(tmp_path: Path) -> Any:
    path = tmp_path / _LOG_NAME
    path.write_bytes(_LOG_BYTES)
    return cli.parse_cameco_log_header(str(path))


def test_3736_is_accepted_and_other_zones_are_not() -> None:
    assert cli.log_crs_declared(3736)
    assert cli.log_crs_declared(32155)
    for other in (3737, 32156, 32613, None):
        assert not cli.log_crs_declared(other)


@pytest.mark.asyncio
async def test_3736_uses_the_ftus_values_as_is(tmp_path: Path) -> None:
    conn = _LogConn()
    await cli.upsert_collar_from_log(
        conn, project_id=_PJ, workspace_id=_WS, parsed=_parsed(tmp_path), source_epsg=3736,  # type: ignore[arg-type]
    )
    assert conn.args is not None
    x, y = conn.args[3], conn.args[4]
    assert (x, y) == (791126.0, 617244.0)
    assert conn.args[8] == 3736
    # GIS-6: the columns keep the file's own numbers.
    assert (conn.args[6], conn.args[7]) == (791126.0, 617244.0)


@pytest.mark.asyncio
async def test_32155_and_3736_place_the_hole_at_the_same_spot(tmp_path: Path) -> None:
    placed = {}
    for epsg in (32155, 3736):
        conn = _LogConn()
        await cli.upsert_collar_from_log(
            conn, project_id=_PJ, workspace_id=_WS, parsed=_parsed(tmp_path), source_epsg=epsg,  # type: ignore[arg-type]
        )
        assert conn.args is not None
        (placed[epsg],) = to_lonlat(epsg, [(conn.args[3], conn.args[4])])
    assert placed[32155] == pytest.approx(placed[3736], abs=1e-7)
    lon, lat = placed[3736]
    assert -106.0 < lon < -104.0 and 41.0 < lat < 43.0   # Shirley Basin area, WY


# ---------------------------------------------------------------------------
# GIS-4 — derive_intervals reads the stored unit instead of assuming feet
# ---------------------------------------------------------------------------


class _CurveRows:
    def __init__(self, depth_unit: str | None) -> None:
        self.depth_unit = depth_unit

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "depth_unit" in sql
        return [{
            "curve_name": "GAMMA", "depths": [10.0, 20.0], "values": [50.0, 60.0],
            "null_value": -999.25, "depth_unit": self.depth_unit,
        }]


@pytest.mark.asyncio
@pytest.mark.parametrize(("unit", "expected"), [
    ("m", [10.0, 20.0]),
    ("ft", [10.0 * FEET_TO_METRES, 20.0 * FEET_TO_METRES]),
])
async def test_derive_uses_the_stored_depth_unit(unit: str, expected: list[float]) -> None:
    from app.services.ingest import derive_intervals as di

    pack = await di._fetch_curve_pack(_CurveRows(unit), "c")  # type: ignore[arg-type]
    assert pack is not None and not pack.depth_unit_unknown
    assert pack.depths_m == pytest.approx(expected)


@pytest.mark.asyncio
async def test_derive_skips_a_legacy_curve_with_no_unit() -> None:
    from app.services.ingest import derive_intervals as di

    pack = await di._fetch_curve_pack(_CurveRows(None), "c")  # type: ignore[arg-type]
    assert pack is not None and pack.depth_unit_unknown and pack.depths_m == []
    out = await di._emit_for_collar(
        _CurveRows(None),  # type: ignore[arg-type]
        workspace_id=_WS, project_id=_PJ, collar_id="c", hole_id="H1",
    )
    assert out == {"hole_id": "H1", "skipped": True, "reason": "depth_unit_unknown"}
