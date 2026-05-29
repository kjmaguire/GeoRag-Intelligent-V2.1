"""Live-stack integration tests for §8 TRG geojson endpoint.

Inserts a synthetic TRG run (zones + recommendations + scores), hits
the /runs/{run_id}/geojson endpoint, asserts the FeatureCollection
shape matches the MapLibre contract, cleans up.

Requires the live Docker stack.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import httpx
import pytest

FASTAPI_URL = os.environ.get("FASTAPI_URL", "http://localhost:8000")
SERVICE_KEY = os.environ.get("FASTAPI_SERVICE_KEY", "georag-service-key-dev")
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def workspace_id(pg_conn: asyncpg.Connection) -> str:
    row = await pg_conn.fetchrow(
        "SELECT workspace_id::text AS w FROM silver.workspaces LIMIT 1",
    )
    if row is None:
        pytest.skip("silver.workspaces is empty")
    return row["w"]


@pytest.fixture
async def project_id(pg_conn: asyncpg.Connection, workspace_id: str) -> str:
    # Find any project in this workspace
    row = await pg_conn.fetchrow(
        "SELECT project_id::text AS p FROM silver.projects WHERE workspace_id = $1::uuid LIMIT 1",
        workspace_id,
    )
    if row is None:
        pytest.skip("no projects in the chosen workspace")
    return row["p"]


@pytest.fixture
async def target_model_id(pg_conn: asyncpg.Connection) -> str:
    row = await pg_conn.fetchrow(
        "SELECT target_model_id::text AS m FROM targeting.target_models LIMIT 1",
    )
    if row is None:
        pytest.skip("targeting.target_models is empty")
    return row["m"]


@pytest.fixture
async def model_version_id(pg_conn: asyncpg.Connection) -> str:
    row = await pg_conn.fetchrow(
        "SELECT version_id::text AS v FROM targeting.target_model_versions LIMIT 1",
    )
    if row is None:
        pytest.skip("targeting.target_model_versions is empty")
    return row["v"]


def _headers() -> dict[str, str]:
    return {"X-Service-Key": SERVICE_KEY}


async def _seed_run(
    conn: asyncpg.Connection,
    workspace_id: str,
    project_id: str,
    target_model_id: str,
    model_version_id: str,
    n_zones: int = 3,
) -> str:
    """Insert a synthetic run with n_zones zones + scores + recommendations.
    Returns the run_id."""
    run_id = str(uuid.uuid4())

    # Polygons spread across central Saskatchewan
    polys = [
        (-105.0 + i * 0.1, 56.0 + i * 0.05) for i in range(n_zones)
    ]
    for rank, (lon, lat) in enumerate(polys, start=1):
        zone_id = str(uuid.uuid4())
        # Insert zone with a small square polygon around the centroid
        await conn.execute(
            """
            INSERT INTO targeting.target_candidate_zones
                (zone_id, workspace_id, project_id, target_model_id,
                 run_id, zone_geom, evidence_payload)
            VALUES
                ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid,
                 ST_GeomFromText($6, 4326), $7::jsonb)
            """,
            zone_id, workspace_id, project_id, target_model_id, run_id,
            f"POLYGON(({lon-0.01} {lat-0.01}, {lon+0.01} {lat-0.01}, "
            f"{lon+0.01} {lat+0.01}, {lon-0.01} {lat+0.01}, {lon-0.01} {lat-0.01}))",
            '{"test":"trg-geojson-it"}',
        )
        score_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO targeting.target_scores
                (score_id, workspace_id, zone_id, model_version_id,
                 aggregate_score, aggregate_uncertainty)
            VALUES
                ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, NULL)
            """,
            score_id, workspace_id, zone_id, model_version_id, 0.9 - rank * 0.1,
        )
        await conn.execute(
            """
            INSERT INTO targeting.target_recommendations
                (recommendation_id, workspace_id, project_id, run_id,
                 zone_id, score_id, rank, explanation_markdown)
            VALUES
                (gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid,
                 $4::uuid, $5::uuid, $6, $7)
            """,
            workspace_id, project_id, run_id, zone_id, score_id, rank,
            f"# Rank {rank}\n\nSynthetic explanation for integration test.",
        )
    return run_id


async def _cleanup(conn: asyncpg.Connection, run_id: str) -> None:
    await conn.execute(
        "DELETE FROM targeting.target_recommendations WHERE run_id = $1::uuid",
        run_id,
    )
    await conn.execute(
        "DELETE FROM targeting.target_candidate_zones WHERE run_id = $1::uuid",
        run_id,
    )
    # Scores have no run_id; clean by evidence_payload tag — orphan scores
    # are harmless if missed but let's be tidy.
    await conn.execute(
        "DELETE FROM targeting.target_scores WHERE workspace_id IN "
        "(SELECT workspace_id FROM silver.workspaces) "
        "AND zone_id NOT IN (SELECT zone_id FROM targeting.target_candidate_zones)",
    )


@pytest.mark.asyncio
async def test_geojson_returns_feature_collection_with_correct_shape(
    pg_conn: asyncpg.Connection,
    workspace_id: str, project_id: str,
    target_model_id: str, model_version_id: str,
) -> None:
    run_id = await _seed_run(
        pg_conn, workspace_id, project_id, target_model_id, model_version_id, n_zones=3,
    )
    try:
        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            r = await client.get(
                f"/api/v1/admin/target_recommendation/runs/{run_id}/geojson",
                headers=_headers(),
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "FeatureCollection"
        assert len(body["features"]) == 3
        # Ordering: rank ascending
        ranks = [f["properties"]["rank"] for f in body["features"]]
        assert ranks == [1, 2, 3]
        # Score field present + parsed as float (not string)
        for f in body["features"]:
            assert "aggregate_score" in f["properties"]
            score = f["properties"]["aggregate_score"]
            assert score is None or isinstance(score, float)
        # Geometry shape
        for f in body["features"]:
            assert f["geometry"]["type"] == "Polygon"
            assert isinstance(f["geometry"]["coordinates"], list)
    finally:
        await _cleanup(pg_conn, run_id)


@pytest.mark.asyncio
async def test_geojson_unknown_run_returns_empty_feature_collection() -> None:
    """An unknown run_id is NOT a 404 — it's a valid empty result.
    The frontend handles 'no zones' as a graceful empty state."""
    fake_run = str(uuid.uuid4())
    async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
        r = await client.get(
            f"/api/v1/admin/target_recommendation/runs/{fake_run}/geojson",
            headers=_headers(),
        )
    assert r.status_code == 200
    assert r.json() == {"type": "FeatureCollection", "features": []}


@pytest.mark.asyncio
async def test_geojson_malformed_run_id_returns_422() -> None:
    """run_id is a path param typed as UUID — bad UUID must 422."""
    async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
        r = await client.get(
            "/api/v1/admin/target_recommendation/runs/not-a-uuid/geojson",
            headers=_headers(),
        )
    assert r.status_code == 422
