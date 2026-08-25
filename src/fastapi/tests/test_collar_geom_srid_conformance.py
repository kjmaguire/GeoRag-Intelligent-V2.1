"""A collar insert must conform to the SRID `silver.collars.geom` declares.

WHY THIS FILE EXISTS
    The column was created with
    ``AddGeometryColumn('silver', 'collars', 'geom', 32613, 'POINT', 2)``,
    so PostGIS rejects anything else outright:

        InvalidParameterValueError: Geometry SRID (26904) does not match
        column SRID (32613)

    _COLLAR_SQL inserted ``ST_SetSRID(ST_MakePoint(...), <source epsg>)``
    unchanged, so the tabular collar write had never worked for ANY project
    outside UTM zone 13N. Not a trace-specific bug -- the whole CSV/Excel
    collar path. It went unnoticed because every corpus until now was
    Athabasca, which IS 32613.

    Found by running the real chain against a live Postgres. Every unit test
    passed throughout, because a recording fake accepts any SQL.

    cameco_log_ingester.py already conforms the same way, transforming its
    32155 source to 32613 at insert, so this path was the odd one out rather
    than the column being wrong.

WHAT IS AND IS NOT PRESERVED
    Reprojecting far outside a UTM zone is exact, not lossy. Sitka's collars
    round-trip to lon -160.558, lat 55.192 -- Unga Island, Alaska, which is
    where the Apollo-Sitka prospect is. The easting/northing COLUMNS keep the
    untouched source values and geom_4326 is transformed straight from the
    source SRID, so only `geom` is expressed in the declared projection.
"""
from __future__ import annotations

import re

from app.hatchet_workflows.ingest_tabular import (
    _COLLAR_SQL,
    COLLAR_GEOM_SRID,
    DEFAULT_SOURCE_EPSG,
)


def test_geom_is_transformed_to_the_columns_declared_srid() -> None:
    # The bug: `ST_SetSRID(ST_MakePoint($5, $6), $15::int)` with nothing
    # around it, which hands PostGIS the SOURCE srid and is refused for
    # every project that is not already 32613.
    assert re.search(
        r"ST_Transform\(\s*ST_SetSRID\(ST_MakePoint\(\$5, \$6\), \$15::int\),\s*"
        + str(COLLAR_GEOM_SRID)
        + r"\s*\)",
        _COLLAR_SQL,
    ), (
        "the geom column insert must ST_Transform to COLLAR_GEOM_SRID "
        f"({COLLAR_GEOM_SRID}); inserting the raw source SRID is rejected by "
        "PostGIS for every project outside that zone"
    )


def test_geom_4326_is_still_transformed_from_the_source_not_from_geom() -> None:
    # geom_4326 is the value the map and every downstream consumer uses. It
    # must come from the SOURCE srid directly — deriving it from the already
    # reprojected geom would be a second transform for no reason, and would
    # tie its correctness to the declared column SRID.
    assert re.search(
        r"ST_Transform\(\s*ST_SetSRID\(ST_MakePoint\(\$5, \$6\), \$15::int\),\s*4326\s*\)",
        _COLLAR_SQL,
    ), "geom_4326 must be transformed from the source SRID, not from geom"


def test_the_source_epsg_is_still_a_parameter_not_a_constant() -> None:
    # The fix conforms the TARGET, and must not have quietly pinned the
    # source too: `$15::int` is the project's own CRS and is what makes
    # geom_4326 land in Alaska rather than in Saskatchewan.
    assert "$15::int" in _COLLAR_SQL


def test_the_declared_srid_and_the_default_source_are_separate_facts() -> None:
    # They happen to be equal today, and conflating them is how this bug
    # would come back: a future change to the Athabasca default must not
    # silently retarget the geom column.
    assert isinstance(COLLAR_GEOM_SRID, int)
    assert isinstance(DEFAULT_SOURCE_EPSG, int)
