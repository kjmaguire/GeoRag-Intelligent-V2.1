"""The LAS placement check across the antimeridian (GIS audit 2026-10, finding 6).

``_within_crs_area`` refused every point of a CRS whose published area crosses
180 degrees (pyproj gives EPSG:4269 as west 167.65 / east -40.73), so a LAS
file with NAD83 coordinates in Saskatchewan, or Alaska Albers coordinates near
Fairbanks, was treated as "not placeable".
"""
from __future__ import annotations

import pytest
from pyproj import Transformer

from app.services.ingest.collar_crs import _within_area
from app.services.ingest.las_ingester import _within_crs_area


def test_nad83_geographic_coordinates_in_saskatchewan_are_placeable() -> None:
    assert _within_crs_area(4269, -106.0, 58.0)


def test_nad27_geographic_coordinates_in_saskatchewan_are_placeable() -> None:
    assert _within_crs_area(4267, -106.0, 58.0)


def test_alaska_albers_coordinates_near_fairbanks_are_placeable() -> None:
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:3338", always_xy=True).transform(-147.72, 64.84)
    assert _within_crs_area(3338, x, y)


def test_the_aleutians_on_the_far_side_of_the_antimeridian_are_placeable() -> None:
    assert _within_crs_area(4269, 175.0, 52.0)


def test_coordinates_that_are_really_outside_are_still_refused() -> None:
    assert not _within_crs_area(4269, 10.0, 50.0)       # Germany labelled NAD83
    assert not _within_crs_area(4269, -106.0, -30.0)    # right longitude, southern hemisphere


def test_an_ordinary_utm_zone_is_unchanged() -> None:
    assert _within_crs_area(26913, 500000.0, 6427000.0)
    # Zone 13 easting 500000 / northing 0 is on the equator, outside the zone's area.
    assert not _within_crs_area(26913, 500000.0, 0.0)


@pytest.mark.parametrize("lon,lat,expected", [(-106.0, 58.0, True), (10.0, 50.0, False)])
def test_collar_crs_uses_the_same_rule(lon: float, lat: float, expected: bool) -> None:
    from pyproj import CRS

    area = CRS.from_epsg(4269).area_of_use
    assert _within_area(area, lon, lat) is expected
