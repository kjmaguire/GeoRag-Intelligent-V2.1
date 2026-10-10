"""silver.coverage_density against a real PostGIS.

GIS audit 2026-10, finding 14, and the database audit's unbounded-grid finding.

* A cell is ``cell_size_m`` metres across (its long axis) on the GROUND, at
  58 N and at 33 S. The function laid its grid out in EPSG:3857 units, which are
  only metres at the equator, so a "1 km" cell was ~531 m at 58 N.
* A record counts in every cell it intersects. ``ST_Contains`` counted only
  records that lay wholly inside ONE cell, so a report outline or a fault - bigger
  than any cell - was counted nowhere.
* The grid is bounded: one record at lon/lat 0,0 (or data on both sides of the
  180th meridian, or an extent too wide for one UTM frame) is refused with
  SQLSTATE 54000 instead of building millions of hexagons.
* The bucketed join returns exactly what the all-pairs ``ST_Intersects`` join
  returns (it is only there to make 62k cells x 20k points take seconds, not
  minutes).

The route that turns the refusal into a 422 is covered without a database in
tests/test_coverage_density_refusal.py.
"""

from __future__ import annotations

import math
import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

from app.db import bind_workspace_scope  # noqa: E402
from app.db.dsn import build_dsn  # noqa: E402

_WORKSPACE = "a0000000-0000-0000-0000-0000000014c5"

#: Saskatchewan, UTM 13N (central meridian 105 W): the latitude the audit measured.
_LON, _LAT = -106.5, 58.25
#: Chile, UTM 19S: the southern-hemisphere zone numbering (327xx) is a separate branch.
_LON_S, _LAT_S = -70.5, -33.0
#: Metres per degree of latitude (spherical), and of longitude at a latitude.
_M_PER_DEG = 111_195.0


def _dlon(metres: float, lat: float) -> float:
    return metres / (_M_PER_DEG * math.cos(math.radians(lat)))


def _dlat(metres: float) -> float:
    return metres / _M_PER_DEG


@pytest.fixture
async def conn():
    c = await asyncpg.connect(build_dsn(), statement_cache_size=0)
    await c.execute(
        """
        INSERT INTO silver.workspaces (workspace_id, name, slug)
        VALUES ($1::uuid, 'coverage-density-pg-tests', 'coverage-density-pg-tests')
        ON CONFLICT (workspace_id) DO NOTHING
        """,
        _WORKSPACE,
    )
    await bind_workspace_scope(c, workspace_id=_WORKSPACE, site="tests.coverage_density_pg", is_local=False)
    try:
        yield c
    finally:
        await c.execute("DELETE FROM silver.collars WHERE workspace_id = $1::uuid", _WORKSPACE)
        await c.execute("DELETE FROM silver.reports WHERE workspace_id = $1::uuid", _WORKSPACE)
        await c.execute("DELETE FROM silver.spatial_features WHERE workspace_id = $1::uuid", _WORKSPACE)
        await c.execute("DELETE FROM silver.projects WHERE workspace_id = $1::uuid", _WORKSPACE)
        await c.close()


async def _project(conn: asyncpg.Connection) -> str:
    project_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO silver.projects (project_id, project_name, slug, workspace_id, crs_datum, status)
        VALUES ($1::uuid, 'coverage-density-pg', 'coverage-density-pg-' || substring($1::text from 1 for 8), $2::uuid,
                'EPSG:4326', 'active')
        """,
        project_id,
        _WORKSPACE,
    )
    return project_id


async def _collars(conn: asyncpg.Connection, project_id: str, points: list[tuple[float, float]]) -> None:
    await conn.executemany(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, project_id, workspace_id, easting, northing, elevation, total_depth,
             hole_type, status, geom_4326)
        VALUES (gen_random_uuid(), $1, $2::uuid, $3::uuid, 0, 0, 0, 100, 'DDH', 'completed',
                ST_SetSRID(ST_MakePoint($4, $5), 4326))
        """,
        [(f"H{i}", project_id, _WORKSPACE, lon, lat) for i, (lon, lat) in enumerate(points)],
    )


async def _report(conn: asyncpg.Connection, project_id: str, wkt: str) -> None:
    await conn.execute(
        """
        INSERT INTO silver.reports (report_id, title, workspace_id, project_id, geom)
        VALUES (gen_random_uuid(), 'outline', $1::uuid, $2::uuid, ST_SetSRID(ST_GeomFromText($3), 4326))
        """,
        _WORKSPACE,
        project_id,
        wkt,
    )


async def _feature(conn: asyncpg.Connection, project_id: str, wkt: str, feature_type: str = "fault") -> None:
    await conn.execute(
        """
        INSERT INTO silver.spatial_features (feature_id, project_id, workspace_id, feature_type, geom)
        VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, ST_SetSRID(ST_GeomFromText($4), 4326))
        """,
        project_id,
        _WORKSPACE,
        feature_type,
        wkt,
    )


def _grid(n: int, spacing_m: float, lon: float, lat: float) -> list[tuple[float, float]]:
    """``n`` x ``n`` points ``spacing_m`` apart, south-west corner at (lon, lat)."""
    return [(lon + _dlon(i * spacing_m, lat), lat + _dlat(j * spacing_m)) for i in range(n) for j in range(n)]


_LONG_AXIS_M = """
    SELECT min(d)::float AS lo, max(d)::float AS hi, count(*) AS cells
      FROM (
        SELECT ST_Distance(ST_PointN(ST_ExteriorRing(cell_polygon), 1)::geography,
                           ST_PointN(ST_ExteriorRing(cell_polygon), 4)::geography) AS d
          FROM silver.coverage_density($1::uuid, $2, $3)
      ) s
"""


# ---------------------------------------------------------------------------
# Cells are cell_size_m metres across on the ground
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cell_size_m", [500, 1000, 5000])
async def test_cells_are_cell_size_metres_across_at_58_north(conn: asyncpg.Connection, cell_size_m: int) -> None:
    project = await _project(conn)
    await _collars(conn, project, _grid(8, 3_000.0, _LON, _LAT))

    row = await conn.fetchrow(_LONG_AXIS_M, project, "collars", cell_size_m)

    assert row["cells"] >= 20
    # UTM's scale factor puts a ground cell within ~0.1 % of nominal; the old
    # 3857 grid was 0.53 x nominal here.
    assert row["lo"] == pytest.approx(cell_size_m, rel=0.005)
    assert row["hi"] == pytest.approx(cell_size_m, rel=0.005)


async def test_cells_are_cell_size_metres_across_in_the_southern_hemisphere(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    await _collars(conn, project, _grid(8, 3_000.0, _LON_S, _LAT_S))

    row = await conn.fetchrow(_LONG_AXIS_M, project, "collars", 1000)

    assert row["cells"] >= 20
    assert row["lo"] == pytest.approx(1000, rel=0.005)
    assert row["hi"] == pytest.approx(1000, rel=0.005)


# ---------------------------------------------------------------------------
# Points: once each, sparse cells flagged
# ---------------------------------------------------------------------------
async def test_each_point_is_counted_once_and_sparse_cells_are_flagged(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    spread = _grid(5, 4_000.0, _LON, _LAT)  # 25 points, each in a cell of its own at 500 m
    cluster = [(_LON + _dlon(50.0 * k, _LAT) + 0.5, _LAT) for k in range(3)]  # 3 holes within 100 m
    await _collars(conn, project, spread + cluster)

    rows = await conn.fetch(
        "SELECT record_count, bias_warning FROM silver.coverage_density($1::uuid, 'collars', 500)", project
    )

    assert sum(r["record_count"] for r in rows) == 28
    counts = sorted(r["record_count"] for r in rows)
    assert counts[-1] == 3, "the three holes within 100 m share a cell"
    assert [r["bias_warning"] for r in rows if r["record_count"] == 3] == [False]
    assert all(r["bias_warning"] for r in rows if r["record_count"] < 3)


async def test_results_are_densest_first(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    await _collars(conn, project, _grid(6, 800.0, _LON, _LAT) + [(_LON, _LAT + 0.0001)] * 4)

    counts = [
        r["record_count"]
        for r in await conn.fetch(
            "SELECT record_count FROM silver.coverage_density($1::uuid, 'collars', 1000)", project
        )
    ]

    assert counts == sorted(counts, reverse=True) and counts[0] > counts[-1]


# ---------------------------------------------------------------------------
# Lines and polygons: counted wherever they intersect (ST_Contains counted none)
# ---------------------------------------------------------------------------
async def test_a_line_is_counted_in_every_cell_it_crosses(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    east = _LON + _dlon(10_000.0, _LAT)
    await _feature(conn, project, f"LINESTRING({_LON} {_LAT}, {east} {_LAT})")

    row = await conn.fetchrow(
        """
        WITH c AS (SELECT cell_polygon, record_count FROM silver.coverage_density($1::uuid, 'spatial_features', 1000)),
             line AS (SELECT ST_SetSRID(ST_MakeLine(ST_MakePoint($2, $3), ST_MakePoint($4, $3)), 4326) AS g)
        SELECT (SELECT count(*) FROM c) AS cells,
               (SELECT max(record_count) FROM c) AS most,
               (SELECT count(*) FROM c, line WHERE ST_Intersects(c.cell_polygon, line.g)) AS crossed,
               (SELECT ST_Length(ST_Difference(line.g, (SELECT ST_Union(cell_polygon) FROM c)))
                       / ST_Length(line.g) FROM line) AS uncovered
        """,
        project,
        _LON,
        _LAT,
        east,
    )

    assert row["cells"] >= 10, "a 10 km line crosses at least 10 one-kilometre cells"
    assert row["cells"] == row["crossed"], "every counted cell is one the line really crosses"
    assert row["most"] == 1
    assert row["uncovered"] < 1e-3, "no stretch of the line falls outside the counted cells"


async def test_a_polygon_bigger_than_a_cell_is_counted_across_its_whole_area(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    east, north = _LON + _dlon(10_000.0, _LAT), _LAT + _dlat(6_000.0)
    await _report(
        conn, project, f"POLYGON(({_LON} {_LAT}, {east} {_LAT}, {east} {north}, {_LON} {north}, {_LON} {_LAT}))"
    )

    row = await conn.fetchrow(
        """
        WITH c AS (SELECT cell_polygon FROM silver.coverage_density($1::uuid, 'reports', 1000)),
             poly AS (SELECT ST_SetSRID(ST_GeomFromText($2), 4326) AS g)
        SELECT (SELECT count(*) FROM c) AS cells,
               ST_Area(ST_Intersection(poly.g, (SELECT ST_Union(cell_polygon) FROM c))) / ST_Area(poly.g) AS covered,
               ST_Area(poly.g::geography) / 649_519.0 AS area_in_cells
          FROM poly
        """,
        project,
        f"POLYGON(({_LON} {_LAT}, {east} {_LAT}, {east} {north}, {_LON} {north}, {_LON} {_LAT}))",
    )

    assert row["covered"] > 0.999, "the counted cells cover the outline"
    assert row["cells"] >= row["area_in_cells"], "at least one cell per cell-area of outline"


async def test_the_old_all_or_nothing_predicate_would_have_counted_neither(conn: asyncpg.Connection) -> None:
    """The line and the outline above are each bigger than any one cell, which is
    what ST_Contains(cell, record) needs. Pin that this is the case, so the two
    tests above keep proving a difference."""
    project = await _project(conn)
    east = _LON + _dlon(10_000.0, _LAT)
    await _feature(conn, project, f"LINESTRING({_LON} {_LAT}, {east} {_LAT})")

    contained = await conn.fetchval(
        """
        WITH c AS (SELECT cell_polygon FROM silver.coverage_density($1::uuid, 'spatial_features', 1000)),
             f AS (SELECT geom FROM silver.spatial_features WHERE project_id = $1::uuid)
        SELECT count(*) FROM c, f WHERE ST_Contains(c.cell_polygon, f.geom)
        """,
        project,
    )

    assert contained == 0


async def test_a_self_intersecting_outline_is_repaired_not_fatal(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    d = _dlon(6_000.0, _LAT)
    h = _dlat(6_000.0)
    # A bow-tie: the classic hand-digitised mistake. ST_IsValid is false for it.
    await _report(
        conn,
        project,
        f"POLYGON(({_LON} {_LAT}, {_LON + d} {_LAT + h}, {_LON} {_LAT + h}, {_LON + d} {_LAT}, {_LON} {_LAT}))",
    )
    assert await conn.fetchval("SELECT NOT ST_IsValid(geom) FROM silver.reports WHERE project_id = $1::uuid", project)

    rows = await conn.fetch("SELECT record_count FROM silver.coverage_density($1::uuid, 'reports', 1000)", project)

    assert len(rows) >= 10 and {r["record_count"] for r in rows} == {1}


# ---------------------------------------------------------------------------
# The bucketed join is the all-pairs join
# ---------------------------------------------------------------------------
async def test_the_bucketed_join_gives_the_all_pairs_answer(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    # 300 pseudo-random points, 3 lines and 2 outlines over ~25 km x 15 km.
    await conn.execute("SELECT setseed(0.31)")
    points = await conn.fetch(
        "SELECT -106.5 + random() * 0.43 AS lon, 58.2 + random() * 0.135 AS lat FROM generate_series(1, 300)"
    )
    await _collars(conn, project, [(r["lon"], r["lat"]) for r in points])
    await _feature(conn, project, "LINESTRING(-106.5 58.2, -106.1 58.3)")
    await _feature(conn, project, "LINESTRING(-106.4 58.33, -106.2 58.21, -106.05 58.25)")
    await _feature(conn, project, "LINESTRING(-106.45 58.2, -106.45 58.33)")
    await _report(conn, project, "POLYGON((-106.4 58.22, -106.25 58.22, -106.25 58.3, -106.4 58.3, -106.4 58.22))")
    await _feature(conn, project, "POLYGON((-106.3 58.2, -106.1 58.25, -106.2 58.32, -106.3 58.2))", "boundary")

    for kind in ("collars", "reports", "spatial_features"):
        mismatching = await conn.fetchval(
            """
            WITH recs AS (
                SELECT ST_MakeValid(geom_4326) AS g FROM silver.collars
                 WHERE $2 = 'collars' AND project_id = $1::uuid AND geom_4326 IS NOT NULL
                UNION ALL SELECT ST_MakeValid(geom) FROM silver.reports
                 WHERE $2 = 'reports' AND project_id = $1::uuid AND geom IS NOT NULL
                UNION ALL SELECT ST_MakeValid(geom) FROM silver.spatial_features
                 WHERE $2 = 'spatial_features' AND project_id = $1::uuid AND geom IS NOT NULL
            ),
            ext AS (SELECT ST_Extent(g)::geometry AS e FROM recs),
            z AS (
                SELECT (CASE WHEN (ST_YMin(e) + ST_YMax(e)) / 2 >= 0 THEN 32600 ELSE 32700 END)
                       + LEAST(60, GREATEST(1, floor(((ST_XMin(e) + ST_XMax(e)) / 2 + 180) / 6)::int + 1)) AS srid
                  FROM ext
            ),
            up AS (SELECT ST_Transform(g, (SELECT srid FROM z)) AS u FROM recs),
            b AS (SELECT ST_SetSRID(ST_Extent(u)::geometry, (SELECT srid FROM z)) AS e FROM up),
            brute AS (
                SELECT round(ST_X(ST_Centroid(ST_Transform(h.geom, 4326)))::numeric, 7) AS clon,
                       round(ST_Y(ST_Centroid(ST_Transform(h.geom, 4326)))::numeric, 7) AS clat,
                       count(*) AS n
                  FROM b, ST_HexagonGrid(500, b.e) h
                  JOIN up ON ST_Intersects(h.geom, up.u)
                 GROUP BY h.geom
            ),
            got AS (
                SELECT round(ST_X(ST_Centroid(cell_polygon))::numeric, 7) AS clon,
                       round(ST_Y(ST_Centroid(cell_polygon))::numeric, 7) AS clat,
                       record_count AS n
                  FROM silver.coverage_density($1::uuid, $2, 1000)
            )
            SELECT count(*) FROM got FULL JOIN brute USING (clon, clat) WHERE got.n IS DISTINCT FROM brute.n
            """,
            project,
            kind,
        )
        assert mismatching == 0, f"{kind}: the bucketed join disagrees with the all-pairs join"


# ---------------------------------------------------------------------------
# Empty and tiny projects
# ---------------------------------------------------------------------------
async def test_an_empty_project_has_no_cells(conn: asyncpg.Connection) -> None:
    project = await _project(conn)

    for kind in ("collars", "reports", "spatial_features"):
        assert await conn.fetch("SELECT 1 FROM silver.coverage_density($1::uuid, $2, 1000)", project, kind) == []


async def test_a_single_point_is_one_sparse_cell(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    await _collars(conn, project, [(_LON, _LAT)])

    rows = await conn.fetch(
        "SELECT record_count, bias_warning FROM silver.coverage_density($1::uuid, 'collars', 1000)", project
    )

    assert [(r["record_count"], r["bias_warning"]) for r in rows] == [(1, True)]


# ---------------------------------------------------------------------------
# The grid is bounded: refuse, do not build it
# ---------------------------------------------------------------------------
async def _refusal(
    conn: asyncpg.Connection, project: str, cell_size_m: int
) -> asyncpg.exceptions.ProgramLimitExceededError:
    with pytest.raises(asyncpg.exceptions.ProgramLimitExceededError) as excinfo:
        await conn.fetch("SELECT 1 FROM silver.coverage_density($1::uuid, 'collars', $2)", project, cell_size_m)
    assert excinfo.value.sqlstate == "54000"
    return excinfo.value


async def test_a_stray_record_at_lon_lat_zero_is_refused(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    await _collars(conn, project, _grid(3, 2_000.0, _LON, _LAT) + [(0.0, 0.0)])

    refusal = await _refusal(conn, project, 500)

    assert "coverage_density" in refusal.message and "cells" in refusal.message
    assert "mis-located" in (refusal.hint or "")


async def test_the_refusal_is_quick(conn: asyncpg.Connection) -> None:
    """It must fail before the grid is built, not after the 300 s statement_timeout."""
    project = await _project(conn)
    await _collars(conn, project, [(_LON, _LAT), (0.0, 0.0)])

    started = await conn.fetchval("SELECT clock_timestamp()")
    await _refusal(conn, project, 500)
    elapsed = (await conn.fetchval("SELECT clock_timestamp()") - started).total_seconds()

    assert elapsed < 5.0


async def test_the_cell_cap_is_on_cells_not_on_area(conn: asyncpg.Connection) -> None:
    near = await _project(conn)  # ~120 km x 120 km at 500 m: ~90k cells, allowed
    await _collars(conn, near, [(_LON, _LAT), (_LON + _dlon(120_000.0, _LAT), _LAT + _dlat(120_000.0))])
    far = await _project(conn)  # ~260 km x 260 km at 500 m: ~420k cells, refused ...
    await _collars(conn, far, [(_LON, _LAT), (_LON + _dlon(260_000.0, _LAT), _LAT + _dlat(260_000.0))])

    allowed = await conn.fetch("SELECT 1 FROM silver.coverage_density($1::uuid, 'collars', 500)", near)
    assert len(allowed) == 2
    await _refusal(conn, far, 500)
    # ... but the same extent is fine at a coarser cell.
    coarse = await conn.fetch("SELECT 1 FROM silver.coverage_density($1::uuid, 'collars', 5000)", far)
    assert len(coarse) == 2


async def test_an_extent_too_wide_for_one_utm_frame_is_refused_whatever_the_cell_size(
    conn: asyncpg.Connection,
) -> None:
    project = await _project(conn)
    # ~2,300 km east-west at 58 N: ~65k cells of 10 km (under the cell cap), but far
    # from any one central meridian, where UTM's scale is several percent off.
    await _collars(conn, project, [(-120.0, 58.0), (-120.0 + _dlon(2_300_000.0, 58.0), 58.0)])

    refusal = await _refusal(conn, project, 10000)

    assert "2000 km" in refusal.message


async def test_data_on_both_sides_of_the_180th_meridian_is_refused(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    await _collars(conn, project, [(179.9, -17.0), (-179.9, -17.0)])

    refusal = await _refusal(conn, project, 10000)

    assert "longitude" in refusal.message and "180th meridian" in (refusal.hint or "")
