"""Tests against a CRS's published area of use, antimeridian-aware.

GIS audit 2026-10. Three places compared a longitude with ``area_of_use``
using ``west <= lon <= east`` (the vector and raster CRS-confidence scores and
the LAS placement check). That is wrong for any CRS whose area CROSSES the
antimeridian, which pyproj publishes as ``west > east``:

    EPSG:4269  NAD83         west  167.65  east  -40.73   (the Aleutians)
    EPSG:4267  NAD27         west  167.65  east  -47.74
    EPSG:3338  Alaska Albers west  172.42  east -129.99

For those, every point in the area is "outside": correctly placed Saskatchewan
data in NAD83 scored 0.0 ("coordinates outside declared CRS extent"), as did
Fairbanks in Alaska Albers, and the confidence reached the user as a wrong
CRS, not as a wrong test.

The rule is the one ``app/services/ingest/collar_crs.py`` already used; it
lives here so the geoparsers (which cannot import app code) and the app share
one implementation.

Longitudes are degrees east in [-180, 180]; an area with ``west > east`` covers
[west, 180] and [-180, east].
"""
from __future__ import annotations

from typing import Any, Literal

BoundsFit = Literal["inside", "partial", "outside"]


def _lon_intervals(west: float, east: float) -> list[tuple[float, float]]:
    """The longitude ranges ``west..east`` covers; two when it wraps 180."""
    if west <= east:
        return [(west, east)]
    return [(west, 180.0), (-180.0, east)]


def within_area(area: Any, lon: float, lat: float, slack_deg: float = 0.0) -> bool:
    """Is a point inside a pyproj ``AreaOfUse``, with ``slack_deg`` of slack?

    ``area`` is anything with ``west`` / ``south`` / ``east`` / ``north``.
    """
    if not (area.south - slack_deg <= lat <= area.north + slack_deg):
        return False
    west, east = area.west - slack_deg, area.east + slack_deg
    if area.west <= area.east:
        return bool(west <= lon <= east)
    return bool(lon >= west or lon <= east)


def classify_bounds(
    area: Any,
    west: float,
    south: float,
    east: float,
    north: float,
) -> BoundsFit:
    """How a lon/lat bounding box sits in an area of use.

    * ``inside``  - the whole box is within the area;
    * ``outside`` - the box does not touch it;
    * ``partial`` - anything else (including a box that spans the antimeridian
      as a world-wide ``west..east``, which cannot be told from a real one).

    The box is ``west <= east`` for ordinary data; a ``west > east`` box is
    read as wrapping, like an area.
    """
    area_ranges = _lon_intervals(area.west, area.east)
    data_ranges = _lon_intervals(west, east)

    lat_inside = area.south <= south and north <= area.north
    lat_touches = not (north < area.south or south > area.north)
    lon_inside = all(
        any(a0 <= d0 and d1 <= a1 for a0, a1 in area_ranges) for d0, d1 in data_ranges
    )
    lon_touches = any(
        d0 <= a1 and a0 <= d1 for d0, d1 in data_ranges for a0, a1 in area_ranges
    )

    if lat_inside and lon_inside:
        return "inside"
    if not (lat_touches and lon_touches):
        return "outside"
    return "partial"
