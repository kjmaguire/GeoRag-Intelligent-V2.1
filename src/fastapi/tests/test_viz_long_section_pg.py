"""The long-section query against a real PostGIS (GIS audit 2026-10, finding 12).

The window count is evaluated over every qualifying hole before ``LIMIT``, and
the metres come out of one local UTM frame. Only a database proves either.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

from app.db.dsn import build_dsn  # noqa: E402
from app.routers import visualizations as viz  # noqa: E402

_WORKSPACE = "a0000000-0000-0000-0000-0000000014c6"
_LON, _LAT = -106.5, 58.25


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(build_dsn(), min_size=1, max_size=2, statement_cache_size=0)
    async with p.acquire() as c:
        await c.execute(
            """
            INSERT INTO silver.workspaces (workspace_id, name, slug)
            VALUES ($1::uuid, 'viz-long-section-pg-tests', 'viz-long-section-pg-tests')
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            _WORKSPACE,
        )
    try:
        yield p
    finally:
        async with p.acquire() as c:
            await c.execute("DELETE FROM silver.collars WHERE workspace_id = $1::uuid", _WORKSPACE)
            await c.execute("DELETE FROM silver.projects WHERE workspace_id = $1::uuid", _WORKSPACE)
        await p.close()


async def _project_with_holes(pool: asyncpg.Pool, n_holes: int) -> uuid.UUID:
    project_id = uuid.uuid4()
    async with pool.acquire() as c:
        await c.execute(
            """
            INSERT INTO silver.projects (project_id, project_name, slug, workspace_id, crs_datum, status)
            VALUES ($1::uuid, 'viz-long-section-pg', 'viz-long-section-pg-' || substring($1::text from 1 for 8),
                    $2::uuid, 'EPSG:4326', 'active')
            """,
            project_id,
            _WORKSPACE,
        )
        await c.executemany(
            """
            INSERT INTO silver.collars
                (collar_id, hole_id, project_id, workspace_id, easting, northing, elevation, total_depth,
                 azimuth, dip, hole_type, status, geom_4326)
            VALUES (gen_random_uuid(), $1, $2::uuid, $3::uuid, 0, 0, 480, 150, 0, -60, 'DDH', 'completed',
                    ST_SetSRID(ST_MakePoint($4, $5), 4326))
            """,
            [
                (f"DDH{i:03d}", project_id, _WORKSPACE, _LON + 0.001 * (i % 13), _LAT + 0.001 * (i // 13))
                for i in range(n_holes)
            ],
        )
    return project_id


async def test_a_project_over_the_cap_reports_its_real_size(pool: asyncpg.Pool) -> None:
    project = await _project_with_holes(pool, 130)

    result = await viz._fetch_long_section_collars(
        pg_pool=pool, workspace_id=_WORKSPACE, project_id=project, reference_azimuth_deg=0.0
    )

    ids = [c["hole_id"] for c in result["collars"]]
    assert len(ids) == viz.LONG_SECTION_MAX_HOLES == 100
    assert ids == sorted(ids) and ids[0] == "DDH000" and ids[-1] == "DDH099"
    assert result["holes_total"] == 130, "the count is taken before the LIMIT"
    assert result["reference_azimuth_deg"] == 0.0


async def test_a_project_under_the_cap_is_not_flagged(pool: asyncpg.Pool) -> None:
    project = await _project_with_holes(pool, 40)

    result = await viz._fetch_long_section_collars(pg_pool=pool, workspace_id=_WORKSPACE, project_id=project)

    assert len(result["collars"]) == result["holes_total"] == 40
    assert result["reference_azimuth_deg"] == 90.0


async def test_eastings_and_northings_are_metres_in_one_local_frame(pool: asyncpg.Pool) -> None:
    project = await _project_with_holes(pool, 26)

    result = await viz._fetch_long_section_collars(pg_pool=pool, workspace_id=_WORKSPACE, project_id=project)

    by_id = {c["hole_id"]: c for c in result["collars"]}
    # DDH000 and DDH013 share a longitude and are 0.001 deg of latitude apart.
    south, north = by_id["DDH000"], by_id["DDH013"]
    # UTM 13N, 58.25 N / 106.5 W: easting ~ 500 000 - 88 km, northing ~ 6.46 million.
    assert 400_000 < south["easting"] < 500_000 and 6_400_000 < south["northing"] < 6_500_000
    # 0.001 deg of latitude is ~111 m on the ground, not 0.001 of anything.
    assert north["northing"] - south["northing"] == pytest.approx(111.2, rel=0.05)
