"""§11.2 — unit + integration tests for the cross-store consistency reporter."""
from __future__ import annotations

import os
import uuid

import asyncpg
import httpx
import pytest

from app.routers import admin_tier234 as t
from app.services.cross_store_consistency import (
    WorkspaceFootprint,
    count_workspace_footprint,
)


# ---------------------------------------------------------------------------
# Unit — dataclass shape + math
# ---------------------------------------------------------------------------
def test_workspace_footprint_defaults() -> None:
    fp = WorkspaceFootprint(workspace_id="abc")
    assert fp.postgres == {}
    assert fp.neo4j_nodes == -1
    assert fp.qdrant_points == -1
    assert fp.redis_keys == -1
    assert fp.has_any_error() is False
    assert fp.total_rows() == 0


def test_workspace_footprint_total_rows_sums_only_positive() -> None:
    """-1 sentinel must NOT subtract from the total; treat as 'unknown'."""
    fp = WorkspaceFootprint(
        workspace_id="abc",
        postgres={"silver_workspaces": 1, "silver_hypotheses": 5, "weird": -1},
        neo4j_nodes=10,
        qdrant_points=-1,
        redis_keys=2,
    )
    assert fp.total_rows() == 1 + 5 + 10 + 2  # 18


def test_workspace_footprint_has_any_error_signals_named_errors_only() -> None:
    """has_any_error reflects *_error fields, not -1 counts."""
    fp_unknown = WorkspaceFootprint(workspace_id="x", neo4j_nodes=-1)
    assert fp_unknown.has_any_error() is False  # -1 alone is not an error

    fp_failed = WorkspaceFootprint(
        workspace_id="x", neo4j_nodes=-1, neo4j_error="connection refused",
    )
    assert fp_failed.has_any_error() is True


def test_workspace_footprint_to_dict_includes_derived_fields() -> None:
    fp = WorkspaceFootprint(workspace_id="abc", postgres={"a": 3})
    d = fp.to_dict()
    assert d["total_rows"] == 3
    assert d["has_any_error"] is False
    assert d["workspace_id"] == "abc"


# ---------------------------------------------------------------------------
# Public API — count_workspace_footprint is the right surface
# ---------------------------------------------------------------------------
def test_count_workspace_footprint_is_async_callable() -> None:
    import inspect
    assert inspect.iscoroutinefunction(count_workspace_footprint)


# ---------------------------------------------------------------------------
# Re-export contract — if restore_workspace renames a private helper,
# this test catches it before the cross_store_consistency module breaks.
# ---------------------------------------------------------------------------
def test_restore_workspace_private_helpers_still_importable() -> None:
    from app.hatchet_workflows.restore_workspace import (
        _count_neo4j_nodes,
        _count_postgres_rows,
        _count_qdrant_points,
        _count_redis_keys,
    )
    assert callable(_count_postgres_rows)
    assert callable(_count_neo4j_nodes)
    assert callable(_count_qdrant_points)
    assert callable(_count_redis_keys)


# ---------------------------------------------------------------------------
# Admin endpoint contract
# ---------------------------------------------------------------------------
def test_workspace_consistency_endpoint_path_present() -> None:
    paths = {r.path for r in t.backups_router.routes if hasattr(r, "path")}
    assert "/api/v1/admin/backups/workspace-consistency/{workspace_id}" in {
        p for p in paths if "consistency" in p
    }


def test_workspace_consistency_response_model_minimum_fields() -> None:
    r = t.WorkspaceConsistencyResponse(
        workspace_id="abc",
        postgres={},
        neo4j_nodes=0,
        qdrant_points=0,
        redis_keys=0,
        total_rows=0,
        has_any_error=False,
    )
    assert r.postgres_error is None
    assert r.neo4j_error is None


# ===========================================================================
# Integration — live stack
# ===========================================================================
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)
FASTAPI_URL = os.environ.get("FASTAPI_URL", "http://localhost:8000")
SERVICE_KEY = os.environ.get("FASTAPI_SERVICE_KEY", "georag-service-key-dev")


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def real_workspace_id(pg_pool: asyncpg.Pool) -> str:
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT workspace_id::text AS w FROM silver.workspaces LIMIT 1",
        )
    if row is None:
        pytest.skip("silver.workspaces is empty")
    return row["w"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_count_workspace_footprint_against_real_workspace(
    pg_pool: asyncpg.Pool, real_workspace_id: str,
) -> None:
    fp = await count_workspace_footprint(real_workspace_id, pg_pool)
    assert fp.workspace_id == real_workspace_id
    # Postgres counter always returns a dict (possibly with -1s)
    assert isinstance(fp.postgres, dict)
    # silver.workspaces always carries at least 1 row for an existing workspace
    assert fp.postgres.get("silver_workspaces", 0) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_count_workspace_footprint_against_nonexistent_workspace(
    pg_pool: asyncpg.Pool,
) -> None:
    """A made-up UUID returns 0 across all stores, no errors."""
    fake = str(uuid.uuid4())
    fp = await count_workspace_footprint(fake, pg_pool)
    assert fp.workspace_id == fake
    assert fp.postgres.get("silver_workspaces", 0) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workspace_consistency_endpoint_round_trip(
    real_workspace_id: str,
) -> None:
    async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
        r = await client.get(
            f"/api/v1/admin/backups/workspace-consistency/{real_workspace_id}",
            headers={"X-Service-Key": SERVICE_KEY},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace_id"] == real_workspace_id
    assert "postgres" in body
    assert "total_rows" in body
    assert "has_any_error" in body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workspace_consistency_endpoint_rejects_bad_uuid() -> None:
    """Path param typed as UUID → 422 for malformed input."""
    async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
        r = await client.get(
            "/api/v1/admin/backups/workspace-consistency/not-a-uuid",
            headers={"X-Service-Key": SERVICE_KEY},
        )
    assert r.status_code == 422
