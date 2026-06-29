"""Live tests for the doc-phase 153 BC MINFILE drillhole adapter."""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.publicgeo.bc_drillhole_adapter import (
    _fetch_bc_drillhole_features,
    sync_bc_drillhole_collars,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


def test_fetch_features_returns_10_bc_drillholes():
    features = _fetch_bc_drillhole_features()
    assert len(features) == 10
    for f in features:
        assert 47 <= f["lat"] <= 61
        assert -141 <= f["lon"] <= -113


@pytest.mark.asyncio
async def test_sync_bc_drillholes_inserts_all_10(pool, conn):
    await conn.execute(
        "DELETE FROM public_geoscience.pg_drillhole_collar "
        "WHERE source_id = 'bc_minfile_drillhole_collar'"
    )
    result = await sync_bc_drillhole_collars(pool=pool)
    assert result.total_features == 10
    assert result.inserted == 10


@pytest.mark.asyncio
async def test_sync_bc_drillholes_idempotent(pool):
    await sync_bc_drillhole_collars(pool=pool)
    result = await sync_bc_drillhole_collars(pool=pool)
    assert result.inserted == 0
    assert result.updated == 0


@pytest.mark.asyncio
async def test_sync_bc_drillholes_emits_audit(pool, conn):
    result = await sync_bc_drillhole_collars(pool=pool)
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'public_geoscience.pull.complete'
           AND id = $1::uuid
        """,
        str(result.audit_ledger_entry_id),
    )
    assert n == 1
