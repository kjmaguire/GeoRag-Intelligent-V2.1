"""Live tests for the doc-phase 149 BC MINFILE adapter."""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.publicgeo.bc_minfile_adapter import (
    _feature_checksum,
    _fetch_bc_minfile_features,
    sync_bc_minfile_mineral_occurrences,
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


# ----------------------------------------------------------------------
# Fetcher + checksum unit tests
# ----------------------------------------------------------------------
def test_fetch_features_returns_realistic_bc_occurrences():
    features = _fetch_bc_minfile_features()
    assert len(features) == 15
    # Each carries the required schema-mapping fields.
    for f in features:
        assert "source_feature_id" in f
        assert "name" in f
        assert "primary_commodities" in f
        assert "status" in f
        assert "lat" in f and "lon" in f
        # All within BC bounding box approximately.
        assert 47 <= f["lat"] <= 61
        assert -141 <= f["lon"] <= -113


def test_feature_checksum_is_deterministic():
    f = {"a": 1, "b": [2, 3], "c": "hello"}
    g = {"c": "hello", "b": [2, 3], "a": 1}  # same content, different key order
    assert _feature_checksum(f) == _feature_checksum(g)


def test_feature_checksum_detects_change():
    f = {"name": "Mine", "status": "producer"}
    g = {"name": "Mine", "status": "past-producer"}
    assert _feature_checksum(f) != _feature_checksum(g)


def test_synthetic_data_covers_multiple_commodity_groupings():
    features = _fetch_bc_minfile_features()
    groupings = {f.get("commodity_grouping") for f in features}
    # Coverage demanded by the §6.5 commodity-aliases UI.
    assert "base_metals" in groupings
    assert "precious_metals" in groupings
    assert "uranium" in groupings
    assert "lithium" in groupings


# ----------------------------------------------------------------------
# End-to-end DB tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sync_bc_minfile_inserts_all_15_features(pool, conn):
    """First sync against a clean state inserts 15 rows."""
    # Clean up any prior runs first.
    await conn.execute(
        "DELETE FROM public_geoscience.pg_mineral_occurrence "
        "WHERE source_id = 'bc_minfile_mineral_occurrence'"
    )

    result = await sync_bc_minfile_mineral_occurrences(pool=pool)
    assert result.total_features == 15
    assert result.inserted == 15
    assert result.updated == 0
    assert result.skipped_no_geom == 0
    assert result.sync_method == "synthetic_stub"

    n = await conn.fetchval(
        "SELECT count(*) FROM public_geoscience.pg_mineral_occurrence "
        "WHERE source_id = 'bc_minfile_mineral_occurrence'"
    )
    assert n == 15

    # All 15 should have geom set.
    n_with_geom = await conn.fetchval(
        "SELECT count(*) FROM public_geoscience.pg_mineral_occurrence "
        "WHERE source_id = 'bc_minfile_mineral_occurrence' AND geom IS NOT NULL"
    )
    assert n_with_geom == 15


@pytest.mark.asyncio
async def test_sync_bc_minfile_is_idempotent(pool, conn):
    """Re-running with unchanged synthetic data → 0 inserted, 0 updated."""
    # Ensure rows are present (first run).
    await sync_bc_minfile_mineral_occurrences(pool=pool)

    # Second run should be a no-op (checksum match).
    result = await sync_bc_minfile_mineral_occurrences(pool=pool)
    assert result.inserted == 0
    assert result.updated == 0


@pytest.mark.asyncio
async def test_sync_bc_minfile_updates_sources_last_refreshed(pool, conn):
    """The sources row's last_refreshed_at bumps after a sync."""
    await sync_bc_minfile_mineral_occurrences(pool=pool)
    ts = await conn.fetchval(
        """
        SELECT last_refreshed_at FROM public_geoscience.sources
         WHERE source_id = 'bc_minfile_mineral_occurrence'
        """
    )
    assert ts is not None


@pytest.mark.asyncio
async def test_sync_bc_minfile_emits_audit_anchor(pool, conn):
    """`public_geoscience.pull.complete` audit row lands."""
    result = await sync_bc_minfile_mineral_occurrences(pool=pool)

    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'public_geoscience.pull.complete'
           AND id = $1::uuid
        """,
        str(result.audit_ledger_entry_id),
    )
    assert n == 1
