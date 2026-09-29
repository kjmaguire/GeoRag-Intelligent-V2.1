"""The collar export's native coordinates no longer come from a 32613 column.

``_fetch_collars`` used to emit ``ST_X/ST_Y/ST_SRID(geom)`` — and
``silver.collars.geom`` was EPSG:32613 for every collar on earth, so an
Alaskan hole left the export as "UTM 13N" metres (-2765464.8, 7604657.1).
The column was retired 2026-09-29; the export now projects ``geom_4326``
into the project's declared projected CRS, else the collar's own UTM zone.

Verified against PostGIS on 2026-09-29 for TR002-Sitka (EPSG:26904 source,
400807 / 6117291): project CRS unset -> EPSG:32604 at 400807.0 / 6117291.0;
project CRS 26904 -> 26904 exactly; project CRS 4269 (geographic) -> 32604.
"""
from __future__ import annotations

import re

import pytest

from app.routers.exports import _COLLAR_EXPORT_SQL


def test_reads_geom_4326_not_the_retired_geom() -> None:
    assert "c.geom_4326" in _COLLAR_EXPORT_SQL
    assert not re.search(r"(?<![\w])c\.geom(?!_)", _COLLAR_EXPORT_SQL)
    assert "ST_SRID(" not in _COLLAR_EXPORT_SQL


def test_no_zone_is_hard_coded() -> None:
    assert "32613" not in _COLLAR_EXPORT_SQL


def test_project_crs_is_used_only_when_it_is_projected() -> None:
    # A geographic project CRS (4326, 4269) must not become the "native"
    # easting/northing — degrees are not what a modelling tool expects in
    # those columns, and the fallback zone is.
    assert "p.crs_epsg" in _COLLAR_EXPORT_SQL
    assert "s.srtext LIKE 'PROJCS%'" in _COLLAR_EXPORT_SQL


def test_the_easting_and_its_epsg_come_from_the_same_transform() -> None:
    assert "ST_X(ST_Transform(c.geom_4326, m.epsg)) AS easting" in _COLLAR_EXPORT_SQL
    assert "ST_Y(ST_Transform(c.geom_4326, m.epsg)) AS northing" in _COLLAR_EXPORT_SQL
    assert "m.epsg                                  AS epsg" in _COLLAR_EXPORT_SQL


def _sql_zone(lon: float, lat: float) -> int:
    """Python mirror of the SQL fallback expression."""
    import math

    base = 32600 if lat >= 0 else 32700
    return base + min(60, max(1, math.floor((lon + 180.0) / 6.0) + 1))


@pytest.mark.parametrize(
    ("lon", "lat", "epsg"),
    [
        (-160.558, 55.192, 32604),  # Sitka, Alaska — zone 4N
        (-105.5, 57.3, 32613),      # Athabasca — zone 13N
        (-106.0, 58.0, 32613),      # still 13N
        (147.0, -6.0, 32755),       # PNG — zone 55S
        (10.7, 59.9, 32632),        # Oslo — zone 32N
        (180.0, 10.0, 32660),       # the antimeridian clamps to zone 60, not 61
        (-180.0, -10.0, 32701),     # zone 1S
    ],
)
def test_fallback_zone_is_the_collars_own_utm_zone(lon: float, lat: float, epsg: int) -> None:
    # Same rule as promote_silver_to_gold._collar_local_utm (the desurvey
    # zone), so a collar gets one metric CRS wherever one is picked for it.
    assert "floor((ST_X(c.geom_4326) + 180.0) / 6.0)::int + 1" in _COLLAR_EXPORT_SQL
    assert "LEAST(60, GREATEST(1," in _COLLAR_EXPORT_SQL
    assert _sql_zone(lon, lat) == epsg
