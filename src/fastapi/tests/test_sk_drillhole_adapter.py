"""Live tests for the doc-phase 152 SK drillhole adapter."""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.publicgeo.sk_drillhole_adapter import (
    _fetch_sk_drillhole_features,
    sync_sk_drillhole_collars,
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


def test_fetch_features_returns_12_drillholes():
    features = _fetch_sk_drillhole_features()
    assert len(features) == 12
    for f in features:
        assert 49 <= f["lat"] <= 60
        assert -110 <= f["lon"] <= -101
        # Drillhole-specific schema fields present.
        assert "drillhole_id" in f
        assert "drill_type" in f


def test_drillholes_have_realistic_lengths():
    features = _fetch_sk_drillhole_features()
    lengths = [f.get("total_length_m") for f in features]
    # Lengths are reasonable mineral-exploration scale.
    for L in lengths:
        if L is not None:
            assert 100 < L < 2500


def test_drillholes_cover_three_drill_types():
    features = _fetch_sk_drillhole_features()
    drill_types = {f.get("drill_type") for f in features}
    assert "diamond_core" in drill_types
    assert "rotary" in drill_types


@pytest.mark.asyncio
async def test_sync_drillholes_inserts_all_12(pool, conn):
    await conn.execute(
        "DELETE FROM public_geoscience.pg_drillhole_collar "
        "WHERE source_id = 'sk_drillhole_collar'"
    )
    result = await sync_sk_drillhole_collars(pool=pool)
    assert result.total_features == 12
    assert result.inserted == 12

    # Verify drillhole-specific columns populated.
    row = await conn.fetchrow(
        "SELECT drillhole_id, drill_type, total_length_m, "
        "       core_availability, commodity_of_interest "
        "  FROM public_geoscience.pg_drillhole_collar "
        " WHERE source_id = 'sk_drillhole_collar' "
        "   AND drillhole_id = 'MR-101'"
    )
    assert row is not None
    assert row["drill_type"] == "diamond_core"
    assert row["total_length_m"] is not None
    assert "U" in row["commodity_of_interest"]


@pytest.mark.asyncio
async def test_sync_drillholes_idempotent(pool):
    await sync_sk_drillhole_collars(pool=pool)
    result = await sync_sk_drillhole_collars(pool=pool)
    assert result.inserted == 0
    assert result.updated == 0


@pytest.mark.asyncio
async def test_sync_drillholes_emits_audit(pool, conn):
    result = await sync_sk_drillhole_collars(pool=pool)
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'public_geoscience.pull.complete'
           AND id = $1::uuid
        """,
        str(result.audit_ledger_entry_id),
    )
    assert n == 1
