"""Live tests for the doc-phase 151 SK mineral occurrence adapter."""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.publicgeo.sk_minoccur_adapter import (
    _fetch_sk_minoccur_features,
    sync_sk_mineral_occurrences,
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


def test_fetch_features_returns_14_sk_occurrences():
    features = _fetch_sk_minoccur_features()
    assert len(features) == 14
    for f in features:
        assert "lat" in f and "lon" in f
        # SK bbox approximately.
        assert 49 <= f["lat"] <= 60
        assert -110 <= f["lon"] <= -101


def test_synthetic_data_covers_three_sk_mineral_provinces():
    features = _fetch_sk_minoccur_features()
    groupings = {f.get("commodity_grouping") for f in features}
    # Athabasca uranium + Trans-Hudson Au/base metals + Southern potash.
    assert "uranium" in groupings
    assert "precious_metals" in groupings
    assert "base_metals" in groupings
    assert "potash_salt" in groupings


def test_synthetic_data_uses_smdi_external_ids():
    features = _fetch_sk_minoccur_features()
    for f in features:
        assert f.get("external_id", "").startswith("SMDI_")


@pytest.mark.asyncio
async def test_sync_sk_inserts_all_14(pool, conn):
    await conn.execute(
        "DELETE FROM public_geoscience.pg_mineral_occurrence "
        "WHERE source_id = 'sk_mineral_occurrence'"
    )
    result = await sync_sk_mineral_occurrences(pool=pool)
    assert result.total_features == 14
    assert result.inserted == 14
    assert result.updated == 0


@pytest.mark.asyncio
async def test_sync_sk_idempotent(pool):
    await sync_sk_mineral_occurrences(pool=pool)
    result = await sync_sk_mineral_occurrences(pool=pool)
    assert result.inserted == 0
    assert result.updated == 0


@pytest.mark.asyncio
async def test_sync_sk_emits_audit(pool, conn):
    result = await sync_sk_mineral_occurrences(pool=pool)
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'public_geoscience.pull.complete'
           AND id = $1::uuid
        """,
        str(result.audit_ledger_entry_id),
    )
    assert n == 1
