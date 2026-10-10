"""The seven MVT tile functions after they gained a 4326 bounding-box prefilter.

GIS audit 2026-10, finding 15. ``ST_Intersects(ST_Transform(col, 3857), tile)``
cannot use the column's GiST index, so every tile request reprojected every row
of the project; the functions now also ask ``col && tile_4326`` (the tile
envelope transformed to 4326 once - exact, because a Web Mercator tile is an
axis-aligned lon/lat rectangle) ahead of the unchanged exact test.

A prefilter that is wrong would silently drop features at tile edges, so the
tests are about WHAT STILL COMES BACK: each of the seven sources returns its
feature for a tile that covers it and nothing for a tile that does not, a line
that crosses a tile boundary is in both tiles, and the exact test is still
there behind the prefilter. (Before/after, the 893 tiles z0-z14 over a 20k-trace
project were byte-identical for all seven functions; that comparison is in the
migration's docblock, not re-run here.)
"""

from __future__ import annotations

import math
import os
import re
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

from app.db import bind_workspace_scope  # noqa: E402
from app.db.dsn import build_dsn  # noqa: E402

_WORKSPACE = "a0000000-0000-0000-0000-0000000014c7"
_LON, _LAT = -106.5, 58.25
_Z = 12

_POINT_FUNCTIONS = ["pg_historic_workings_by_project", "pg_geochem_by_project"]
#: Lines and polygons: a feature can straddle a tile edge, so it belongs to both tiles.
_AREA_FUNCTIONS = [
    "pg_drill_traces_by_project",
    "pg_seismic_by_project",
    "pg_boundaries_by_project",
    "pg_formations_by_project",
    "pg_cross_section_lines_by_project",
]
_FUNCTIONS = [*_AREA_FUNCTIONS, *_POINT_FUNCTIONS]


def _tile(lon: float, lat: float, z: int = _Z) -> tuple[int, int]:
    n = 2.0**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def _tile_west_edge(x: int, z: int = _Z) -> float:
    return x / (2.0**z) * 360.0 - 180.0


@pytest.fixture
async def conn():
    c = await asyncpg.connect(build_dsn(), statement_cache_size=0)
    await c.execute(
        """
        INSERT INTO silver.workspaces (workspace_id, name, slug)
        VALUES ($1::uuid, 'mvt-prefilter-pg-tests', 'mvt-prefilter-pg-tests')
        ON CONFLICT (workspace_id) DO NOTHING
        """,
        _WORKSPACE,
    )
    await bind_workspace_scope(c, workspace_id=_WORKSPACE, site="tests.mvt_prefilter_pg", is_local=False)
    try:
        yield c
    finally:
        for table in (
            "silver.drill_traces",
            "silver.seismic_surveys",
            "silver.project_boundaries",
            "silver.geological_formations",
            "silver.historic_workings",
            "silver.geochemistry",
            "gold.cross_section_panels",
            "silver.collars",
            "silver.projects",
        ):
            await c.execute(f"DELETE FROM {table} WHERE workspace_id = $1::uuid", _WORKSPACE)  # noqa: S608
        await c.close()


async def _seed(conn: asyncpg.Connection, lon: float, lat: float, half_m: float = 0.0003) -> str:
    """One feature in each of the seven sources, all within ~±half_m degrees of (lon, lat)."""
    project = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO silver.projects (project_id, project_name, slug, workspace_id, crs_datum, status)
        VALUES ($1::uuid, 'mvt-prefilter-pg', 'mvt-prefilter-pg-' || substring($1::text from 1 for 8), $2::uuid,
                'EPSG:4326', 'active')
        """,
        project,
        _WORKSPACE,
    )
    collar = str(uuid.uuid4())
    h = half_m
    sq = f"{lon - h} {lat - h}, {lon + h} {lat - h}, {lon + h} {lat + h}, {lon - h} {lat + h}, {lon - h} {lat - h}"
    await conn.execute(
        """
        INSERT INTO silver.collars (collar_id, hole_id, project_id, workspace_id, easting, northing, elevation,
                                    total_depth, hole_type, status, geom_4326)
        VALUES ($1::uuid, 'T1', $2::uuid, $3::uuid, 0, 0, 0, 100, 'DDH', 'completed',
                ST_SetSRID(ST_MakePoint($4, $5), 4326))
        """,
        collar,
        project,
        _WORKSPACE,
        lon,
        lat,
    )
    await conn.execute(
        """
        INSERT INTO silver.drill_traces (collar_id, workspace_id, project_id, geom, survey_hash)
        VALUES ($1::uuid, $2::uuid, $3::uuid,
                ST_SetSRID(ST_GeomFromText($4), 4326), repeat('a', 64))
        """,
        collar,
        _WORKSPACE,
        project,
        f"LINESTRING Z({lon - h} {lat - h} 500, {lon + h} {lat + h} 450)",
    )
    await conn.execute(
        """
        INSERT INTO silver.seismic_surveys (survey_id, survey_name, survey_type, num_traces, num_samples_per_trace,
                                            sample_interval_us, record_length_ms, source_file, file_size_bytes,
                                            project_id, workspace_id, bbox)
        VALUES (gen_random_uuid(), 'S', '2D', 1, 1, 1, 1, 'f.sgy', 1, $1::uuid, $2::uuid,
                ST_SetSRID(ST_GeomFromText($3), 4326))
        """,
        project,
        _WORKSPACE,
        f"POLYGON(({sq}))",
    )
    await conn.execute(
        """
        INSERT INTO silver.project_boundaries (workspace_id, project_id, boundary_name, boundary_type, geom)
        VALUES ($1::uuid, $2::uuid, 'B', 'claim', ST_Multi(ST_SetSRID(ST_GeomFromText($3), 4326)))
        """,
        _WORKSPACE,
        project,
        f"POLYGON(({sq}))",
    )
    await conn.execute(
        """
        INSERT INTO silver.geological_formations (workspace_id, project_id, formation_code, formation_name, geom)
        VALUES ($1::uuid, $2::uuid, 'F', 'Formation', ST_Multi(ST_SetSRID(ST_GeomFromText($3), 4326)))
        """,
        _WORKSPACE,
        project,
        f"POLYGON(({sq}))",
    )
    await conn.execute(
        """
        INSERT INTO silver.historic_workings (workspace_id, project_id, working_type, geom)
        VALUES ($1::uuid, $2::uuid, 'shaft', ST_SetSRID(ST_MakePoint($3, $4), 4326))
        """,
        _WORKSPACE,
        project,
        lon,
        lat,
    )
    await conn.execute(
        """
        INSERT INTO silver.geochemistry (geochem_id, project_id, workspace_id, geom)
        VALUES (gen_random_uuid(), $1::uuid, $2::uuid, ST_SetSRID(ST_MakePoint($3, $4), 4326))
        """,
        project,
        _WORKSPACE,
        lon,
        lat,
    )
    await conn.execute(
        """
        INSERT INTO gold.cross_section_panels (workspace_id, project_id, section_name, section_line_geom)
        VALUES ($1::uuid, $2::uuid, 'L', ST_SetSRID(ST_GeomFromText($3), 4326))
        """,
        _WORKSPACE,
        project,
        f"LINESTRING({lon - h} {lat}, {lon + h} {lat})",
    )
    return project


async def _tile_bytes(conn: asyncpg.Connection, fn: str, project: str, x: int, y: int, z: int = _Z) -> bytes:
    row = await conn.fetchrow(
        f"SELECT mvt FROM silver.{fn}($1, $2, $3, $4::json)",  # noqa: S608
        z,
        x,
        y,
        f'{{"project_id": "{project}", "workspace_id": "{_WORKSPACE}"}}',
    )
    return bytes(row["mvt"] or b"")


@pytest.mark.parametrize("fn", _FUNCTIONS)
async def test_a_source_is_in_the_tile_that_covers_it_and_not_in_a_far_one(conn: asyncpg.Connection, fn: str) -> None:
    project = await _seed(conn, _LON, _LAT)
    x, y = _tile(_LON, _LAT)

    assert await _tile_bytes(conn, fn, project, x, y), f"{fn}: the covering tile came back empty"
    assert not await _tile_bytes(conn, fn, project, x + 5, y), f"{fn}: a tile 5 columns east is not empty"
    assert not await _tile_bytes(conn, fn, project, x, y + 5), f"{fn}: a tile 5 rows south is not empty"


@pytest.mark.parametrize("fn", _AREA_FUNCTIONS)
async def test_a_line_or_polygon_across_a_tile_edge_is_in_both_tiles(conn: asyncpg.Connection, fn: str) -> None:
    """The prefilter is a bounding-box test in 4326: it must keep what straddles the edge."""
    x, y = _tile(_LON, _LAT)
    edge = _tile_west_edge(x + 1)  # the boundary between tile x and tile x+1
    # +-0.002 deg is ~+-120 m at 58 N: well inside one tile on either side of the edge.
    project = await _seed(conn, edge, _LAT, half_m=0.002)

    west = await _tile_bytes(conn, fn, project, x, y)
    east = await _tile_bytes(conn, fn, project, x + 1, y)
    far = await _tile_bytes(conn, fn, project, x - 1, y)

    assert west, f"{fn}: nothing in the tile west of the edge"
    assert east, f"{fn}: nothing in the tile east of the edge"
    assert not far, f"{fn}: a tile two away from the edge is not empty"


@pytest.mark.parametrize("fn", _POINT_FUNCTIONS)
async def test_a_point_just_either_side_of_a_tile_edge_is_in_its_own_tile_only(
    conn: asyncpg.Connection, fn: str
) -> None:
    x, y = _tile(_LON, _LAT)
    edge = _tile_west_edge(x + 1)
    just_east = await _seed(conn, edge + 0.0002, _LAT, half_m=0.00001)  # ~12 m east of the edge
    just_west = await _seed(conn, edge - 0.0002, _LAT, half_m=0.00001)  # ~12 m west of it

    assert await _tile_bytes(conn, fn, just_east, x + 1, y), (
        f"{fn}: the point east of the edge is missing from its tile"
    )
    assert not await _tile_bytes(conn, fn, just_east, x, y), f"{fn}: the point east of the edge is in the west tile"
    assert await _tile_bytes(conn, fn, just_west, x, y), f"{fn}: the point west of the edge is missing from its tile"
    assert not await _tile_bytes(conn, fn, just_west, x + 1, y), f"{fn}: the point west of the edge is in the east tile"


@pytest.mark.parametrize("fn", _FUNCTIONS)
async def test_the_other_workspace_cannot_see_the_tile(conn: asyncpg.Connection, fn: str) -> None:
    """The prefilter sits beside the tenant predicate, not in place of it."""
    project = await _seed(conn, _LON, _LAT)
    x, y = _tile(_LON, _LAT)

    row = await conn.fetchrow(
        f"SELECT mvt FROM silver.{fn}($1, $2, $3, $4::json)",  # noqa: S608
        _Z,
        x,
        y,
        f'{{"project_id": "{project}", "workspace_id": "a0000000-0000-0000-0000-0000000014ff"}}',
    )

    assert not row["mvt"], f"{fn}: a workspace that does not own the project got its tile"


@pytest.mark.parametrize("fn", _FUNCTIONS)
async def test_the_definition_prefilters_and_keeps_the_exact_test(conn: asyncpg.Connection, fn: str) -> None:
    definition = await conn.fetchval(
        "SELECT pg_get_functiondef(to_regprocedure($1))", f"silver.{fn}(integer,integer,integer,json)"
    )

    assert "tile_4326 := ST_Transform(tile_bbox, 4326)" in definition
    assert "&& tile_4326" in definition, f"{fn}: no bounding-box prefilter on the 4326 column"
    assert definition.index("&& tile_4326") < definition.index("ST_Intersects("), "prefilter comes first"
    assert re.search(r"ST_Intersects\(\s*ST_Transform\([a-z_.]+, 3857\),\s*tile_bbox\s*\)", definition), (
        "the exact 3857 test is still there"
    )
    assert "workspace_id = v_wsid" in definition, "workspace scoping untouched"
