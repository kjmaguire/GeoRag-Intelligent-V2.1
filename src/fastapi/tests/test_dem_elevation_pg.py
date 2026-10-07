"""``promote_silver_to_gold._fill_terrain_elevations`` against a real PostGIS.

The unit tests in ``test_dem_elevation.py`` drive the step through a scripted
connection, so none of its SQL ever executes there: a typo in the unnest
UPDATE, the stale-clearing predicate or the float8 assignment would leave
every one of them green while the production step became the silent no-op its
``except Exception`` allows. This runs the real statements; only the remote
tile read is replaced (no network in CI).
"""

from __future__ import annotations

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
from app.services import dem_elevation as dem  # noqa: E402

_WORKSPACE = "a0000000-0000-0000-0000-0000000de111"
_RED_STAR = (-160.559, 55.192)  # Unga Island, Alaska


@pytest.fixture
async def conn(monkeypatch: pytest.MonkeyPatch):
    for name in (dem.URL_TEMPLATE_ENV, dem.SOURCE_ENV, dem.TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    c = await asyncpg.connect(build_dsn(), statement_cache_size=0)
    await c.execute(
        """
        INSERT INTO silver.workspaces (workspace_id, name, slug)
        VALUES ($1::uuid, 'dem-pg-tests', 'dem-pg-tests')
        ON CONFLICT (workspace_id) DO NOTHING
        """,
        _WORKSPACE,
    )
    await bind_workspace_scope(c, workspace_id=_WORKSPACE, site="tests.dem_elevation_pg", is_local=False)
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
        VALUES ($1::uuid, 'dem-pg', 'dem-pg-' || substring($1::text from 1 for 8), $2::uuid, 'EPSG:4326', 'active')
        """,
        project_id,
        _WORKSPACE,
    )
    return project_id


async def _collar(
    conn: asyncpg.Connection, project_id: str, hole: str, lon: float, lat: float, elevation: float | None = None
) -> str:
    collar_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, project_id, workspace_id, easting, northing,
             elevation, hole_type, status, geom_4326)
        VALUES ($1::uuid, $2, $3::uuid, $4::uuid, 0, 0, $5, 'DDH', 'completed',
                ST_SetSRID(ST_MakePoint($6, $7), 4326))
        """,
        collar_id,
        hole,
        project_id,
        _WORKSPACE,
        elevation,
        lon,
        lat,
    )
    return collar_id


def _heights_by_lat(monkeypatch: pytest.MonkeyPatch, table: dict[float, float | None]) -> list:
    """Replace the remote read: height by latitude (so the answer follows the point)."""
    calls: list = []

    async def fake(points, config=None, deadline=None):  # noqa: ANN001, ANN202
        calls.append(list(points))
        return {i: table[round(lat, 3)] for i, (_, lat) in enumerate(points) if round(lat, 3) in table}

    monkeypatch.setattr(dem, "lookup_elevations", fake)
    return calls


async def _row(conn: asyncpg.Connection, collar_id: str):
    return await conn.fetchrow(
        """
        SELECT elevation, elevation_dem_m, elevation_dem_source,
               ST_AsText(elevation_dem_geom) AS dem_at,
               COALESCE(elevation, elevation_dem_m) AS effective,
               pg_typeof(elevation_dem_m)::text AS dem_type
          FROM silver.collars WHERE collar_id = $1::uuid
        """,
        collar_id,
    )


async def _fill(conn: asyncpg.Connection, project_id: str) -> m.PromoteSilverToGoldOutput:
    out = m.PromoteSilverToGoldOutput()
    await m._fill_terrain_elevations(
        conn, project_id=project_id, out=out, budget=dem.TerrainBudget(run_s=1000, project_s=100)
    )
    return out


async def test_fills_stamps_and_is_idempotent(conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    project = await _project(conn)
    land = await _collar(conn, project, "R1", _RED_STAR[0], 55.192)
    sea = await _collar(conn, project, "R2", _RED_STAR[0], 55.193)
    calls = _heights_by_lat(monkeypatch, {55.192: 12.9, 55.193: None})

    out = await _fill(conn, project)
    assert (out.collars_terrain_elevation_filled, out.collars_terrain_no_ground) == (1, 1)

    row = await _row(conn, land)
    assert row["elevation"] is None
    assert row["elevation_dem_m"] == 12.9  # double precision: no float4 tail
    assert row["dem_type"] == "double precision"
    assert row["elevation_dem_source"] == "copernicus_glo30"
    assert row["dem_at"] == "POINT(-160.559 55.192)"
    sea_row = await _row(conn, sea)
    assert sea_row["elevation_dem_m"] is None and sea_row["dem_at"] is not None  # looked up: no ground

    # A second run has nothing pending and makes no lookup.
    await _fill(conn, project)
    assert len(calls) == 1


async def test_a_moved_collar_is_cleared_and_looked_up_again(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _project(conn)
    cid = await _collar(conn, project, "R1", _RED_STAR[0], 55.192)
    calls = _heights_by_lat(monkeypatch, {55.192: 12.9, 56.5: 300.0})
    await _fill(conn, project)

    await conn.execute(
        "UPDATE silver.collars SET geom_4326 = ST_SetSRID(ST_MakePoint(-160.559, 56.5), 4326) WHERE collar_id = $1::uuid",
        cid,
    )
    # Until the next promotion the old height must not be what a reader sees.
    stale = await conn.fetchval(
        f"SELECT {dem.EFFECTIVE_ELEVATION_SQL} FROM silver.collars c WHERE c.collar_id = $1::uuid", cid
    )
    assert stale is None

    await _fill(conn, project)
    assert len(calls) == 2
    assert (await _row(conn, cid))["elevation_dem_m"] == 300.0


async def test_a_changed_source_label_looks_everything_up_again(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _project(conn)
    cid = await _collar(conn, project, "R1", _RED_STAR[0], 55.192)
    calls = _heights_by_lat(monkeypatch, {55.192: 12.9})
    await _fill(conn, project)
    monkeypatch.setenv(dem.SOURCE_ENV, "my_lidar")
    await _fill(conn, project)
    assert len(calls) == 2
    assert (await _row(conn, cid))["elevation_dem_source"] == "my_lidar"


async def test_a_file_elevation_always_wins(conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    project = await _project(conn)
    surveyed = await _collar(conn, project, "R1", _RED_STAR[0], 55.192, elevation=62.0)
    bare = await _collar(conn, project, "R2", _RED_STAR[0], 55.193)
    # Reference check: the model says 50 m where the survey says 62 m (canopy, not a datum).
    _heights_by_lat(monkeypatch, {55.192: 50.0, 55.193: 45.0})

    await _fill(conn, project)
    assert (await _row(conn, surveyed))["elevation_dem_m"] is None  # never written for a surveyed collar
    assert (await _row(conn, surveyed))["effective"] == 62.0
    assert (await _row(conn, bare))["effective"] == 45.0


async def test_a_local_grid_project_is_left_alone_and_earlier_fills_are_withdrawn(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _project(conn)
    bare = await _collar(conn, project, "R2", _RED_STAR[0], 55.193)
    _heights_by_lat(monkeypatch, {55.193: 45.0})
    await _fill(conn, project)
    assert (await _row(conn, bare))["elevation_dem_m"] == 45.0

    # Later a surveyed collar arrives with a mine-grid RL (height + 1000 m).
    await _collar(conn, project, "R1", _RED_STAR[0], 55.192, elevation=1045.0)
    calls = _heights_by_lat(monkeypatch, {55.192: 45.0, 55.193: 45.0})
    out = await _fill(conn, project)

    row = await _row(conn, bare)
    assert row["elevation_dem_m"] is None and row["dem_at"] is None  # withdrawn
    assert out.collars_terrain_datum_mismatch == 1
    assert [len(points) for points in calls] == [1]  # only the reference was sampled


async def test_nothing_is_filled_when_the_surveyed_collars_cannot_be_read(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _project(conn)
    bare = await _collar(conn, project, "R2", _RED_STAR[0], 55.193)
    await _collar(conn, project, "R1", _RED_STAR[0], 55.192, elevation=1045.0)
    _heights_by_lat(monkeypatch, {})  # host unreachable
    out = await _fill(conn, project)
    assert out.collars_terrain_elevation_filled == 0
    assert (await _row(conn, bare))["dem_at"] is None


async def test_the_range_check_constraint_rejects_a_nodata_sentinel(conn: asyncpg.Connection) -> None:
    project = await _project(conn)
    cid = await _collar(conn, project, "R1", _RED_STAR[0], 55.192)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE silver.collars SET elevation_dem_m = -32767 WHERE collar_id = $1::uuid", cid)
