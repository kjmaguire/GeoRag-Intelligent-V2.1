"""GIS audit 2026-09-29 — the tabular CRS decision and plausibility checks.

GIS-1: a lon/lat collar table was stamped with the project's UTM EPSG (or
32613) and every hole landed within metres of the equator. GIS-2: an
undeclared projected table is still placed at 32613, but flagged
``assumed``. GIS-13/15: positions are checked against the project and the
CRS's area of use, and implausible ones WARN.

Pure module, no database and no Hatchet client: runs everywhere.
"""
from __future__ import annotations

import pytest

from app.services.ingest.collar_crs import (
    AOI_WARN_KM,
    ProjectReference,
    decide_collar_crs,
    haversine_km,
    plausibility_warnings,
    to_lonlat,
)

#: Real Athabasca-area positions (Key Lake road), WGS 84.
_LON = [-105.6231, -105.6187, -105.6102]
_LAT = [57.2104, 57.2139, 57.2188]


def _decide(**kw):
    base = dict(
        eastings=_LON, northings=_LAT,
        easting_column="Longitude", northing_column="Latitude",
        declared_epsg=None, project_epsg=None, default_epsg=32613, label="t.csv",
    )
    base.update(kw)
    return decide_collar_crs(**base)


# ---------------------------------------------------------------------------
# GIS-1
# ---------------------------------------------------------------------------


def test_the_bug_lat_lon_read_as_utm_lands_on_the_equator() -> None:
    # The failure this module exists to stop, proved with pyproj.
    (lon, lat), = to_lonlat(32613, [(-105.5, 57.3)])
    assert abs(lat) < 0.01


def test_lat_lon_table_in_a_utm_project_is_placed_as_4326() -> None:
    decision = _decide(project_epsg=32613)
    assert decision.epsg == 4326
    assert decision.georef_method == "detected"
    assert decision.assumed is False
    assert [w["code"] for w in decision.warnings] == ["collar_crs_geographic_detected"]
    # And 4326 puts the hole where it was drilled.
    (lon, lat), = to_lonlat(decision.epsg, [(_LON[0], _LAT[0])])
    assert (lon, lat) == pytest.approx((_LON[0], _LAT[0]))


def test_lat_lon_by_values_with_x_y_headers() -> None:
    decision = _decide(easting_column="X", northing_column="Y", project_epsg=26913)
    assert decision.epsg == 4326
    assert decision.crs_confidence < 0.8   # value-based, a little less sure


def test_lat_lon_with_no_declaration_is_not_assumed_utm() -> None:
    decision = _decide()
    assert decision.epsg == 4326
    assert decision.assumed is False


def test_declared_geographic_datum_is_honoured() -> None:
    decision = _decide(declared_epsg=4267)
    assert decision.epsg == 4267
    assert decision.georef_method == "declared"
    assert decision.warnings == []


def test_declared_projected_epsg_overridden_for_degrees_with_warning() -> None:
    decision = _decide(declared_epsg=26913)
    assert decision.epsg == 4326
    assert decision.warnings[0]["code"] == "collar_crs_geographic_override"
    assert "EPSG:26913" in decision.warnings[0]["detail"]


def test_declared_geographic_epsg_with_projected_values_refuses() -> None:
    decision = _decide(
        eastings=[512000.0], northings=[6340000.0],
        easting_column="Easting", northing_column="Northing",
        declared_epsg=4326,
    )
    assert decision.refusal is not None
    assert decision.refusal["code"] == "collar_crs_mismatch"


def test_local_grid_is_not_read_as_degrees() -> None:
    decision = _decide(
        eastings=[10.0, 55.5, 110.0], northings=[5.0, 40.25, 85.0],
        easting_column="Local_X", northing_column="Local_Y",
        project_epsg=26913,
    )
    assert decision.epsg == 26913


# ---------------------------------------------------------------------------
# GIS-2
# ---------------------------------------------------------------------------


def test_undeclared_projected_is_assumed_default() -> None:
    decision = _decide(
        eastings=[512000.0], northings=[6340000.0],
        easting_column="Easting", northing_column="Northing",
    )
    assert decision.epsg == 32613
    assert decision.assumed is True
    assert decision.georef_method == "assumed"


def test_project_crs_is_a_declaration() -> None:
    decision = _decide(
        eastings=[512000.0], northings=[6340000.0],
        easting_column="Easting", northing_column="Northing",
        project_epsg=26904,
    )
    assert (decision.epsg, decision.georef_method, decision.assumed) == (26904, "declared", False)


def test_feet_coordinate_headers_under_metre_crs_warn() -> None:
    decision = _decide(
        eastings=[791126.0], northings=[617244.0],
        easting_column="Easting_ft", northing_column="Northing_ft",
        project_epsg=32155,
    )
    assert "collar_crs_unit_mismatch" in [w["code"] for w in decision.warnings]
    # A feet-based system is consistent with the header: no warning.
    ok = _decide(
        eastings=[791126.0], northings=[617244.0],
        easting_column="Easting_ft", northing_column="Northing_ft",
        project_epsg=3736,
    )
    assert "collar_crs_unit_mismatch" not in [w["code"] for w in ok.warnings]


# ---------------------------------------------------------------------------
# GIS-13 plausibility
# ---------------------------------------------------------------------------

_REF = ProjectReference(-110.2, 57.0, 0.0, "the project's 12 other placed collar(s)")


def test_wrong_utm_zone_is_flagged_against_the_project() -> None:
    # Audit proof: (-110.2, 57.0) in zone 12 is 548598E 6317670N. Read as
    # zone 13 it lands at (-104.2, 57.0), ~364 km east, and is INSIDE
    # 32613's area of use, so only the project reference catches it.
    warnings, flagged = plausibility_warnings(
        epsg=32613, points=[("H-1", 548598.0, 6317670.0)],
        reference=_REF, label="t.csv",
    )
    codes = [w["code"] for w in warnings]
    assert codes == ["collar_far_from_project"]
    assert flagged == {"H-1"}


def test_correct_zone_is_not_flagged() -> None:
    warnings, flagged = plausibility_warnings(
        epsg=32612, points=[("H-1", 548598.0, 6317670.0)],
        reference=_REF, label="t.csv",
    )
    assert warnings == [] and flagged == set()


def test_degrees_read_as_utm_fall_outside_the_zone() -> None:
    warnings, flagged = plausibility_warnings(
        epsg=32613, points=[("H-1", -105.5, 57.3)], reference=None, label="t.csv",
    )
    assert "collar_outside_crs_area" in [w["code"] for w in warnings]
    assert flagged == {"H-1"}


def test_positive_west_longitude_names_the_hemisphere() -> None:
    warnings, _ = plausibility_warnings(
        epsg=4326, points=[("W-1", 105.6, 57.2)],
        reference=ProjectReference(-105.6, 57.2, 0.0, "the project boundary"),
        label="well.las",
    )
    far = next(w for w in warnings if w["code"] == "collar_far_from_project")
    assert "opposite hemisphere" in far["detail"]


def test_boundary_radius_widens_the_tolerance() -> None:
    ref = ProjectReference(-105.0, 57.0, 150.0, "the project boundary")
    lon_off = -105.0 + 3.0  # ~181 km east at 57 N: inside radius + AOI
    assert haversine_km(-105.0, 57.0, lon_off, 57.0) < 150.0 + AOI_WARN_KM
    warnings, _ = plausibility_warnings(
        epsg=4326, points=[("H", lon_off, 57.0)], reference=ref, label="t",
    )
    assert warnings == []


def test_untransformable_point_is_reported() -> None:
    warnings, flagged = plausibility_warnings(
        epsg=4326, points=[("H", 512000.0, 6340000.0)], reference=None, label="t",
    )
    assert warnings[0]["code"] == "collar_position_untransformable"
    assert flagged == {"H"}
