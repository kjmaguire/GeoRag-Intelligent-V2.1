"""The promote step's drill-trace SQL against a real PostGIS.

GIS audit 2026-10, finding 1. The unit tests drive ``_promote_traces`` through
a scripted connection, so the ST_Translate / ST_Transform that puts the metre
offsets onto the collar never runs there.

* The line stored in ``silver.drill_traces`` must START AT THE COLLAR even when
  the survey's first reading is 30 m down.
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
from app.hatchet_workflows import promote_silver_to_gold as m  # noqa: E402

_WORKSPACE = "a0000000-0000-0000-0000-0000000610c5"
_LON, _LAT, _ELEV = -106.5, 58.25, 512.0


@pytest.fixture
async def conn():
    c = await asyncpg.connect(build_dsn(), statement_cache_size=0)
    await c.execute(
        """
        INSERT INTO silver.workspaces (workspace_id, name, slug)
        VALUES ($1::uuid, 'promote-gis-pg-tests', 'promote-gis-pg-tests')
        ON CONFLICT (workspace_id) DO NOTHING
        """,
        _WORKSPACE,
    )
    await bind_workspace_scope(c, workspace_id=_WORKSPACE, site="tests.promote_gis_pg", is_local=False)
    try:
        yield c
    finally:
        await c.execute("DELETE FROM silver.collars WHERE workspace_id = $1::uuid", _WORKSPACE)
        await c.execute("DELETE FROM silver.projects WHERE workspace_id = $1::uuid", _WORKSPACE)
        await c.close()


async def _project(conn: asyncpg.Connection) -> str:
    project_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO silver.projects (project_id, project_name, slug, workspace_id, crs_datum, status)
        VALUES ($1::uuid, 'promote-gis-pg', 'promote-gis-pg-' || substring($1::text from 1 for 8), $2::uuid,
                'EPSG:4326', 'active')
        """,
        project_id,
        _WORKSPACE,
    )
    return project_id


async def _collar(conn: asyncpg.Connection, project_id: str, hole: str, *, td: float = 200.0) -> str:
    collar_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, project_id, workspace_id, easting, northing, elevation, total_depth,
             hole_type, status, geom_4326)
        VALUES ($1::uuid, $2, $3::uuid, $4::uuid, 0, 0, $5, $6, 'DDH', 'completed',
                ST_SetSRID(ST_MakePoint($7, $8), 4326))
        """,
        collar_id,
        hole,
        project_id,
        _WORKSPACE,
        _ELEV,
        td,
        _LON,
        _LAT,
    )
    return collar_id


async def _survey(conn: asyncpg.Connection, collar_id: str, stations: list[tuple[float, float, float]]) -> None:
    for depth, azimuth, dip in stations:
        await conn.execute(
            """
            INSERT INTO silver.surveys (survey_id, collar_id, workspace_id, depth, azimuth, dip,
                                        survey_method, source_file)
            VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5, 'downhole', 'surveys.csv')
            """,
            collar_id,
            _WORKSPACE,
            depth,
            azimuth,
            dip,
        )


# ---------------------------------------------------------------------------
# Finding 1 - the stored line starts at the collar
# ---------------------------------------------------------------------------
async def test_the_stored_trace_starts_at_the_collar_when_the_first_reading_is_30_m_down(
    conn: asyncpg.Connection,
) -> None:
    project = await _project(conn)
    collar = await _collar(conn, project, "T1")
    await _survey(conn, collar, [(30.0, 90.0, -60.0), (100.0, 90.0, -60.0), (200.0, 90.0, -60.0)])

    out = m.PromoteSilverToGoldOutput()
    await m._promote_traces(conn, workspace_id=_WORKSPACE, project_id=project, out=out)
    assert out.traces_written == 1

    row = await conn.fetchrow(
        """
        SELECT ST_NPoints(t.geom) AS n,
               ST_X(ST_StartPoint(t.geom)) AS lon, ST_Y(ST_StartPoint(t.geom)) AS lat,
               ST_Z(ST_StartPoint(t.geom)) AS z,
               ST_X(c.geom_4326) AS clon, ST_Y(c.geom_4326) AS clat,
               ST_SRID(t.geom) AS srid
          FROM silver.drill_traces t JOIN silver.collars c USING (collar_id)
         WHERE t.collar_id = $1::uuid
        """,
        collar,
    )
    assert row["srid"] == 4326
    assert row["n"] == 4, "the collar plus the three survey stations"
    assert row["lon"] == pytest.approx(row["clon"], abs=1e-9)
    assert row["lat"] == pytest.approx(row["clat"], abs=1e-9)
    assert row["z"] == pytest.approx(_ELEV)


async def test_the_second_vertex_is_30_m_along_the_first_stations_attitude(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    collar = await _collar(conn, project, "T2")
    await _survey(conn, collar, [(30.0, 90.0, -60.0), (200.0, 90.0, -60.0)])
    await m._promote_traces(conn, workspace_id=_WORKSPACE, project_id=project, out=m.PromoteSilverToGoldOutput())

    row = await conn.fetchrow(
        """
        SELECT ST_Z(ST_PointN(t.geom, 2)) AS z,
               ST_Distance(ST_StartPoint(t.geom)::geography, ST_PointN(t.geom, 2)::geography) AS plan_m
          FROM silver.drill_traces t WHERE t.collar_id = $1::uuid
        """,
        collar,
    )
    # Due east at -60: 30 * cos(60) = 15 m in plan, 30 * sin(60) = 25.98 m down.
    assert row["plan_m"] == pytest.approx(15.0, abs=0.05)
    assert row["z"] == pytest.approx(_ELEV - 30.0 * math.sin(math.radians(60.0)), abs=1e-6)


async def test_a_trace_written_by_the_collarless_builder_is_replaced_on_the_next_run(
    conn: asyncpg.Connection,
) -> None:
    project = await _project(conn)
    collar = await _collar(conn, project, "T3")
    stations = [(30.0, 90.0, -60.0), (200.0, 90.0, -60.0)]
    await _survey(conn, collar, stations)

    # What the version-2 builder left in the table: no collar vertex, and the
    # digest that version computed.
    original = m._TRACE_BUILDER_VERSION
    try:
        m._TRACE_BUILDER_VERSION = 2
        stale = m._survey_hash(stations, origin=(_LON, _LAT, _ELEV))
    finally:
        m._TRACE_BUILDER_VERSION = original
    await conn.execute(
        """
        INSERT INTO silver.drill_traces (collar_id, workspace_id, project_id, geom, survey_hash, trace_quality)
        VALUES ($1::uuid, $2::uuid, $3::uuid,
                ST_GeomFromText('LINESTRING Z (-106.4999 58.25 486.0, -106.498 58.25 365.0)', 4326), $4, 'ok')
        """,
        collar,
        _WORKSPACE,
        project,
        stale,
    )

    out = m.PromoteSilverToGoldOutput()
    await m._promote_traces(conn, workspace_id=_WORKSPACE, project_id=project, out=out)

    assert (out.traces_written, out.traces_unchanged) == (1, 0)
    assert await conn.fetchval("SELECT ST_NPoints(geom) FROM silver.drill_traces WHERE collar_id = $1::uuid", collar) == 3
