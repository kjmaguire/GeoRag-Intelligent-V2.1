"""Live-Postgres checks for the agent's spatial SQL (database audit PG-11).

geospatial_planner builds SQL from SPATIAL_TARGETS at runtime, so no static
scan of string literals can see it. Every one of the 15 target x operation
plans used to fail PREPARE with UndefinedColumn (`spatial_crs`,
`total_depth_m`, `feature_label`, `smdi_id`, ...), and project_geometry read
`silver.projects.bbox` / `silver.collars.collar_geom`, which do not exist,
then swallowed the first error inside its transaction so the fallback could
never run. No intent profile routes to the tool today; this keeps it from
being broken the day one does.

Skip-safe module-level guard on POSTGRES_USER, same convention as
test_csv_collar_ingester_integration.py.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

import uuid  # noqa: E402
from typing import get_args  # noqa: E402

import asyncpg  # noqa: E402

from app.agent.geospatial_planner import (  # noqa: E402
    SPATIAL_TARGETS,
    SpatialOperation,
    SpatialQuerySpec,
    plan_spatial_query,
)
from app.agent.project_geometry import get_project_bbox_wkt  # noqa: E402


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.mark.parametrize("operation", get_args(SpatialOperation))
@pytest.mark.parametrize("target", sorted(SPATIAL_TARGETS))
async def test_every_plan_prepares(target: str, operation: str) -> None:
    plan = plan_spatial_query(
        SpatialQuerySpec(
            target=target,
            operation=operation,  # type: ignore[arg-type]
            geometry_wkt="POINT(-105 55)",
            buffer_m=1000.0 if operation == "dwithin" else None,
        ),
    )
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await conn.prepare(plan.sql)
    finally:
        await conn.close()


@pytest.fixture
async def project_ids():
    ws = str(uuid.uuid4())
    with_boundary = str(uuid.uuid4())
    collars_only = str(uuid.uuid4())
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1::uuid, $2, $3)",
            ws, "spatial-plans-it", f"spatial-plans-it-{ws[:8]}",
        )
        for pid, label in ((with_boundary, "boundary"), (collars_only, "collars")):
            await conn.execute(
                """
                INSERT INTO silver.projects
                    (project_id, project_name, slug, workspace_id, crs_datum,
                     orientation_reference, status)
                VALUES ($1::uuid, $2, $3, $4::uuid, 'EPSG:4326', 'BOH', 'active')
                """,
                pid, f"spatial-{label}", f"spatial-{label}-{pid[:8]}", ws,
            )
        await conn.execute(
            """
            UPDATE silver.projects
               SET geom_boundary = ST_GeomFromText(
                   'POLYGON((-106 54,-104 54,-104 56,-106 56,-106 54))', 4326)
             WHERE project_id = $1::uuid
            """,
            with_boundary,
        )
        for i, (lon, lat) in enumerate(((-105.5, 55.1), (-105.1, 55.4))):
            await conn.execute(
                """
                INSERT INTO silver.collars
                    (collar_id, hole_id, project_id, workspace_id, easting, northing,
                     total_depth, hole_type, status, geom_4326)
                VALUES (gen_random_uuid(), $1, $2::uuid, $3::uuid, $4, $5, 100, 'DD',
                        'complete', ST_SetSRID(ST_MakePoint($4, $5), 4326))
                """,
                f"SP-{i}", collars_only, ws, lon, lat,
            )
        yield ws, with_boundary, collars_only
    finally:
        await conn.execute("DELETE FROM silver.projects WHERE workspace_id = $1::uuid", ws)
        await conn.execute("DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", ws)
        await conn.close()


async def test_project_bbox_from_boundary_then_from_collars(project_ids) -> None:
    ws, with_boundary, collars_only = project_ids
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=1, statement_cache_size=0)
    try:
        boundary_wkt = await get_project_bbox_wkt(pool, workspace_id=ws, project_id=with_boundary)
        envelope_wkt = await get_project_bbox_wkt(pool, workspace_id=ws, project_id=collars_only)
    finally:
        await pool.close()

    assert boundary_wkt is not None and boundary_wkt.startswith("POLYGON((-106 54")
    assert envelope_wkt is not None and envelope_wkt.startswith("POLYGON((-105.5 55.1")
