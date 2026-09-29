"""A LAS file must never be given a location the data does not support.

WHY THIS FILE EXISTS
    Tracing the "Red Star" import found that ``las_ingester`` created a
    collar for any well it did not already know, at ``DEFAULT_UTM_FALLBACK``
    (480000, 4660000 in EPSG:32613 -- Carbon County, Wyoming) whenever the
    PLSS section was not in its one-entry reference table, with no warning
    and no georef_method. A hole from anywhere was drawn in Wyoming.

    The rule now, in order: an existing collar; coordinates in the LAS
    header; a PLSS section that resolves (the Cameco Shirley Basin flow),
    flagged ``assumed`` with a warning; otherwise the file is refused with
    ``las_collar_unlocated``. silver.collars.easting / northing are NOT NULL,
    so "a collar with no location" is not on offer and the schema is untouched.

The connection is a recording fake; SQL validity against the live schema is
the integration bucket's job.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lasio")

from app.services.ingest import las_ingester  # noqa: E402
from app.services.ingest.las_ingester import (  # noqa: E402
    PLSS_UNCERTAINTY_M,
    ingest_las_file,
)

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_COLLAR = "d3000000-0000-0000-0000-0000000000c0"
_EXISTING = "e4000000-0000-0000-0000-0000000000e1"


class _Conn:
    """Records the collar INSERT and curve writes; answers the lookups."""

    def __init__(
        self,
        *,
        by_hole_id: str | None = None,
        by_canonical: str | None = None,
        project_crs: Any = None,
    ) -> None:
        self.by_hole_id = by_hole_id
        self.by_canonical = by_canonical
        self.project_crs = project_crs
        self.collar_insert: tuple[Any, ...] | None = None
        self.curve_writes = 0
        self.provenance_writes = 0

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "crs_epsg" in sql:
            return self.project_crs
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, str] | None:
        flat = " ".join(sql.split())
        if flat.startswith("SELECT collar_id") and "hole_id_canonical" in flat:
            return {"collar_id": self.by_canonical} if self.by_canonical else None
        if flat.startswith("SELECT collar_id"):
            return {"collar_id": self.by_hole_id} if self.by_hole_id else None
        if "INSERT INTO silver.collars" in flat:
            self.collar_insert = args
            return {"collar_id": _COLLAR}
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO silver.well_log_curves" in sql:
            self.curve_writes += 1
        if "INSERT INTO bronze.provenance" in sql:
            self.provenance_writes += 1
        return "OK"


def _insert(conn: _Conn) -> dict[str, Any]:
    """Name the positional args of the silver.collars INSERT."""
    assert conn.collar_insert is not None, "no collar was inserted"
    names = (
        "hole_id", "hole_id_canonical", "project_id", "easting", "northing",
        "total_depth", "drill_date", "georef_method", "uncertainty_m",
        "uncertainty_method", "source_x", "source_y", "source_epsg",
        "workspace_id",
    )
    return dict(zip(names, conn.collar_insert, strict=True))


def _las(
    path: Path, *, well: str = "SB-001", stop: str = "100.0",
    extra_well: str = "", loc: str = "", state: str = "WY",
) -> Path:
    loc_line = f"LOC  .  {loc} : LOCATION\n" if loc else ""
    path.write_text(
        "~VERSION INFORMATION\n"
        " VERS.                 2.0 : CWLS LOG ASCII STANDARD - VERSION 2.0\n"
        " WRAP.                  NO : ONE LINE PER DEPTH STEP\n"
        "~WELL INFORMATION\n"
        "STRT .F          0.0 : START DEPTH\n"
        f"STOP .F   {stop} : STOP DEPTH\n"
        "STEP .F          0.5 : STEP\n"
        "NULL .       -999.25 : NULL VALUE\n"
        "COMP .  CAMECO : COMPANY\n"
        f"WELL .  {well} : WELL\n"
        "FLD  .  Shirley Basin : FIELD\n"
        "CNTY .  CARBON : COUNTY\n"
        f"STAT .  {state} : STATE\n"
        f"{loc_line}"
        f"{extra_well}"
        "DATE .  08/13/2012 : DATE\n"
        "~CURVE INFORMATION\n"
        "DEPT .F  : DEPTH\n"
        "GAMMA.API : GAMMA RAY\n"
        "~ASCII\n"
        "0.0   12.0\n"
        "0.5   14.0\n"
        "1.0   15.0\n",
        encoding="ascii",
    )
    return path


def _codes(result: Any) -> list[str]:
    return [w["code"] for w in result.warnings]


# ---------------------------------------------------------------------------
# (a) coordinates in the LAS header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_header_latlon_with_datum_is_declared_and_warning_free(tmp_path: Path) -> None:
    las = _las(
        tmp_path / "h.las",
        extra_well="LATI .DEG   42.06 : LATITUDE\nLONG .DEG -105.35 : LONGITUDE\nGDAT .   NAD83 : DATUM\n",
    )
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert not result.skipped, result.skipped_reason
    assert result.georef_method == "declared"
    assert result.warnings == []
    row = _insert(conn)
    assert row["georef_method"] == "declared"
    assert (row["source_x"], row["source_y"], row["source_epsg"]) == (-105.35, 42.06, 4269)
    # easting/northing hold METRES in the collar SRID, never degrees.
    assert 300_000 < row["easting"] < 700_000
    assert 4_000_000 < row["northing"] < 5_000_000
    assert conn.curve_writes == 1  # GAMMA (DEPT is the index)


@pytest.mark.asyncio
async def test_header_latlon_without_datum_is_assumed_wgs84_with_a_warning(tmp_path: Path) -> None:
    las = _las(
        tmp_path / "h.las",
        extra_well="LATI .DEG   42.06 : LATITUDE\nLONG .DEG -105.35 : LONGITUDE\n",
    )
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.georef_method == "assumed"
    assert _codes(result) == ["las_collar_crs_assumed"]
    assert "WGS84" in result.warnings[0]["detail"]
    assert _insert(conn)["source_epsg"] == 4326


@pytest.mark.asyncio
async def test_header_xy_with_an_epsg_item_is_declared(tmp_path: Path) -> None:
    las = _las(
        tmp_path / "h.las",
        extra_well="X    .M    471234.5 : X\nY    .M   4657321.0 : Y\nEPSG .    26913 : CRS\n",
    )
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.georef_method == "declared"
    assert result.warnings == []
    row = _insert(conn)
    assert (row["source_x"], row["source_y"], row["source_epsg"]) == (471234.5, 4657321.0, 26913)
    assert (row["easting"], row["northing"]) == (471234.5, 4657321.0)


@pytest.mark.asyncio
async def test_header_xy_takes_the_operators_declared_epsg(tmp_path: Path) -> None:
    las = _las(
        tmp_path / "h.las",
        extra_well="EAST .M    471234.5 : E\nNORTH.M   4657321.0 : N\n",
    )
    conn = _Conn(project_crs=32613)

    result = await ingest_las_file(
        conn, str(las), workspace_id=_WS, project_id_override=_PJ, source_epsg=32613,
    )

    assert result.georef_method == "declared"
    assert result.warnings == []


@pytest.mark.asyncio
async def test_header_xy_with_no_crs_anywhere_uses_the_project_crs_and_says_so(tmp_path: Path) -> None:
    las = _las(
        tmp_path / "h.las",
        extra_well="X    .M    471234.5 : X\nY    .M   4657321.0 : Y\n",
    )
    conn = _Conn(project_crs=32613)

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.georef_method == "assumed"
    assert _codes(result) == ["las_collar_crs_assumed"]
    assert "project's CRS" in result.warnings[0]["detail"]
    assert _insert(conn)["source_epsg"] == 32613


@pytest.mark.asyncio
async def test_header_xy_with_no_crs_and_no_project_crs_is_refused(tmp_path: Path) -> None:
    las = _las(
        tmp_path / "h.las",
        extra_well="X    .M    471234.5 : X\nY    .M   4657321.0 : Y\n",
    )
    conn = _Conn(project_crs=None)

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.skipped and result.skipped_reason == "collar_unlocated"
    assert "no CRS is stated" in result.warnings[0]["detail"]
    assert conn.collar_insert is None


@pytest.mark.asyncio
async def test_header_xy_outside_the_crs_area_is_not_trusted(tmp_path: Path) -> None:
    """Feet read as metres, a wrong zone and swapped axes are all finite."""
    las = _las(
        tmp_path / "h.las",
        # Swapped axes: northing 4.66 Mm read as an easting in UTM 13N.
        extra_well="X    .M   4657321.0 : X\nY    .M    471234.5 : Y\nEPSG .    26913 : CRS\n",
    )
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.skipped and result.skipped_reason == "collar_unlocated"
    assert "do not fall inside the area" in result.warnings[0]["detail"]
    assert conn.collar_insert is None


@pytest.mark.asyncio
async def test_null_coordinates_in_the_header_are_not_coordinates(tmp_path: Path) -> None:
    """Cameco files carry LAT/LON = 'NA'; that must read as absent."""
    las = _las(
        tmp_path / "h.las",
        extra_well="LATI .DEG NA : LATITUDE\nLONG .DEG NA : LONGITUDE\n",
    )
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.skipped and result.skipped_reason == "collar_unlocated"
    assert conn.collar_insert is None


# ---------------------------------------------------------------------------
# (b) PLSS -- the Cameco Shirley Basin flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resolving_plss_section_still_creates_the_collar_but_flags_it_assumed(tmp_path: Path) -> None:
    las = _las(tmp_path / "s.las", well="36-1042", loc="36    28    79")
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert not result.skipped, result.skipped_reason
    assert result.collar_id == _COLLAR
    assert result.georef_method == "assumed"
    assert _codes(result) == ["las_collar_assumed_location"]
    detail = result.warnings[0]["detail"]
    assert "36-1042" in detail and "028N079W36" in detail and "s.las" in detail
    row = _insert(conn)
    assert row["georef_method"] == "assumed"
    # Same Shirley Basin box the pre-fix flow produced.
    assert 470_000 < row["easting"] < 472_000
    assert 4_656_000 < row["northing"] < 4_658_000
    assert row["source_epsg"] == 32613
    assert row["uncertainty_m"] == PLSS_UNCERTAINTY_M
    assert row["uncertainty_method"] == "plss_section_centroid"
    assert conn.curve_writes == 1


@pytest.mark.asyncio
async def test_a_callers_plss_key_still_works(tmp_path: Path) -> None:
    """cluster_runner passes plss_section_key; a tabulated key resolves."""
    las = _las(tmp_path / "s.las", well="A-1")
    conn = _Conn()

    result = await ingest_las_file(
        conn, str(las), workspace_id=_WS, project_id_override=_PJ,
        plss_section_key="028N079W36",
    )

    assert result.georef_method == "assumed"
    assert _codes(result) == ["las_collar_assumed_location"]


@pytest.mark.asyncio
async def test_header_coordinates_beat_a_plss_guess(tmp_path: Path) -> None:
    las = _las(
        tmp_path / "s.las", well="36-1042", loc="36 28 79",
        extra_well="LATI .DEG   42.06 : LATITUDE\nLONG .DEG -105.35 : LONGITUDE\nGDAT .  WGS84 : D\n",
    )
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.georef_method == "declared"
    assert result.warnings == []


# ---------------------------------------------------------------------------
# (c) nothing resolves -> refuse, do not fabricate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_coordinates_and_no_plss_is_refused_with_a_named_warning(tmp_path: Path) -> None:
    las = _las(tmp_path / "Sitka_hole7.las", well="SIT-007")
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.skipped and result.skipped_reason == "collar_unlocated"
    assert result.collar_id is None and result.curves_inserted == 0
    assert _codes(result) == ["las_collar_unlocated"]
    detail = result.warnings[0]["detail"]
    assert "SIT-007" in detail and "Sitka_hole7.las" in detail
    assert "Upload the collar table first" in detail
    # Nothing was written: no collar, no curves, no provenance.
    assert conn.collar_insert is None
    assert conn.curve_writes == 0 and conn.provenance_writes == 0


@pytest.mark.asyncio
async def test_a_plss_section_missing_from_the_table_is_refused_not_defaulted(tmp_path: Path) -> None:
    """This is the old DEFAULT_UTM_FALLBACK path, verbatim."""
    las = _las(tmp_path / "x.las", well="X-1", loc="12    33    90")
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.skipped and result.skipped_reason == "collar_unlocated"
    assert "033N090W12" in result.warnings[0]["detail"]
    assert conn.collar_insert is None


@pytest.mark.asyncio
async def test_no_code_path_writes_the_old_wyoming_default() -> None:
    assert not hasattr(las_ingester, "DEFAULT_UTM_FALLBACK")
    assert las_ingester._derive_coordinates(None, "H-1") is None
    assert las_ingester._derive_coordinates("999N999W99", "H-1") is None


# ---------------------------------------------------------------------------
# An existing collar is used as it stands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_existing_collar_takes_the_curves_and_no_new_collar_is_made(tmp_path: Path) -> None:
    las = _las(tmp_path / "e.las", well="SIT-007")
    conn = _Conn(by_hole_id=_EXISTING)

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert not result.skipped
    assert result.collar_id == _EXISTING
    assert result.warnings == [] and result.georef_method is None
    assert conn.collar_insert is None
    assert conn.curve_writes == 1


@pytest.mark.asyncio
async def test_a_canonical_hole_id_match_finds_the_collar(tmp_path: Path) -> None:
    """LAS 'SRE09_6' vs the collar table's 'SRE09-6'."""
    las = _las(tmp_path / "e.las", well="SRE09_6")
    conn = _Conn(by_canonical=_EXISTING)

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.collar_id == _EXISTING
    assert conn.collar_insert is None


# ---------------------------------------------------------------------------
# STOP <= 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["0.0", "-5.0"])
async def test_a_non_positive_stop_is_a_warning_naming_the_file(tmp_path: Path, stop: str) -> None:
    las = _las(tmp_path / "bad_stop.las", well="SB-9", stop=stop, loc="36 28 79")
    conn = _Conn()

    result = await ingest_las_file(conn, str(las), workspace_id=_WS, project_id_override=_PJ)

    assert result.skipped and result.skipped_reason == "invalid_total_depth"
    assert _codes(result) == ["las_invalid_stop_depth"]
    detail = result.warnings[0]["detail"]
    assert "bad_stop.las" in detail and "SB-9" in detail and "STOP" in detail
    assert conn.collar_insert is None and conn.curve_writes == 0
