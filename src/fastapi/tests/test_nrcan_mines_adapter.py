"""Live tests for the doc-phase 150 NRCan Canadian Mines adapter."""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.publicgeo.nrcan_mines_adapter import (
    _fetch_nrcan_mines_features,
    sync_nrcan_canadian_mines,
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
# Fetcher unit tests
# ----------------------------------------------------------------------
def test_fetch_features_returns_12_mines():
    features = _fetch_nrcan_mines_features()
    assert len(features) == 12
    for f in features:
        assert "source_feature_id" in f
        assert "name" in f
        assert "commodities" in f
        assert "lat" in f and "lon" in f
        # Canada bbox sanity.
        assert 41 <= f["lat"] <= 84
        assert -141 <= f["lon"] <= -52


def test_synthetic_mines_span_multiple_provinces():
    features = _fetch_nrcan_mines_features()
    provinces = {f.get("province") for f in features}
    # The seed covers BC, SK, ON, QC, NL, NT — federal coverage view.
    assert "BC" in provinces
    assert "SK" in provinces
    assert "ON" in provinces
    assert len(provinces) >= 5


def test_synthetic_mines_cover_commodity_groups():
    features = _fetch_nrcan_mines_features()
    groupings = {f.get("commodity_grouping") for f in features}
    # Federal view needs precious + base + uranium + ree + potash + coal + gemstones.
    expected = {"precious_metals", "base_metals", "uranium", "ree", "potash_salt", "coal", "gemstones"}
    assert expected.issubset(groupings)


# ----------------------------------------------------------------------
# End-to-end DB tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sync_nrcan_mines_inserts_all_12(pool, conn):
    """Clean state → 12 inserts in pg_mine."""
    await conn.execute(
        "DELETE FROM public_geoscience.pg_mine "
        "WHERE source_id = 'nrcan_canadian_mines'"
    )
    result = await sync_nrcan_canadian_mines(pool=pool)
    assert result.total_features == 12
    assert result.inserted == 12
    assert result.updated == 0
    assert result.sync_method == "synthetic_stub"

    n = await conn.fetchval(
        "SELECT count(*) FROM public_geoscience.pg_mine "
        "WHERE source_id = 'nrcan_canadian_mines'"
    )
    assert n == 12


@pytest.mark.asyncio
async def test_sync_nrcan_mines_idempotent(pool):
    await sync_nrcan_canadian_mines(pool=pool)
    result = await sync_nrcan_canadian_mines(pool=pool)
    assert result.inserted == 0
    assert result.updated == 0


@pytest.mark.asyncio
async def test_sync_nrcan_mines_emits_audit(pool, conn):
    result = await sync_nrcan_canadian_mines(pool=pool)
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'public_geoscience.pull.complete'
           AND id = $1::uuid
        """,
        str(result.audit_ledger_entry_id),
    )
    assert n == 1
