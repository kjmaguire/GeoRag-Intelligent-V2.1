"""Live tests for doc-phase 155 AB + NRCan bedrock_geology adapters
— closes the §6 PublicGeo adapter set (9 of 9)."""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.publicgeo.bedrock_geology_adapters import (
    sync_ab_ags_bedrock_geology,
    sync_nrcan_geo_bedrock_geology,
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


@pytest.mark.asyncio
async def test_sync_ab_bedrock_inserts_all_units(pool, conn):
    await conn.execute(
        "DELETE FROM public_geoscience.pg_bedrock_geology "
        "WHERE source_id = 'ab_ags_bedrock_geology'"
    )
    result = await sync_ab_ags_bedrock_geology(pool=pool)
    assert result.total_features == 8
    assert result.inserted == 8

    # Verify the McMurray bitumen sands unit landed.
    row = await conn.fetchrow(
        "SELECT unit_name, era, period, lithology "
        "  FROM public_geoscience.pg_bedrock_geology "
        " WHERE source_id = 'ab_ags_bedrock_geology' "
        "   AND unit_code = 'ABFB'"
    )
    assert row is not None
    assert "McMurray" in row["unit_name"]
    assert row["era"] == "Mesozoic"
    assert "bitumen" in row["lithology"]


@pytest.mark.asyncio
async def test_sync_nrcan_bedrock_inserts_all_units(pool, conn):
    await conn.execute(
        "DELETE FROM public_geoscience.pg_bedrock_geology "
        "WHERE source_id = 'nrcan_geo_bedrock_geology'"
    )
    result = await sync_nrcan_geo_bedrock_geology(pool=pool)
    assert result.total_features == 8
    assert result.inserted == 8

    # Verify Canadian Shield + WCSB units present.
    n = await conn.fetchval(
        "SELECT count(*) FROM public_geoscience.pg_bedrock_geology "
        "WHERE source_id = 'nrcan_geo_bedrock_geology' "
        "  AND unit_code IN ('PCSH', 'WCSB', 'CORO')"
    )
    assert n == 3


@pytest.mark.asyncio
async def test_both_adapters_idempotent(pool):
    await sync_ab_ags_bedrock_geology(pool=pool)
    await sync_nrcan_geo_bedrock_geology(pool=pool)
    r1 = await sync_ab_ags_bedrock_geology(pool=pool)
    r2 = await sync_nrcan_geo_bedrock_geology(pool=pool)
    assert r1.inserted == 0 and r1.updated == 0
    assert r2.inserted == 0 and r2.updated == 0


@pytest.mark.asyncio
async def test_bedrock_geom_persists_as_multipolygon(pool, conn):
    await sync_ab_ags_bedrock_geology(pool=pool)
    geom_type = await conn.fetchval(
        "SELECT GeometryType(geom) FROM public_geoscience.pg_bedrock_geology "
        "WHERE source_id = 'ab_ags_bedrock_geology' LIMIT 1"
    )
    assert geom_type == "MULTIPOLYGON"


@pytest.mark.asyncio
async def test_bedrock_adapters_emit_audit(pool, conn):
    r1 = await sync_ab_ags_bedrock_geology(pool=pool)
    r2 = await sync_nrcan_geo_bedrock_geology(pool=pool)
    for entry_id in (r1.audit_ledger_entry_id, r2.audit_ledger_entry_id):
        n = await conn.fetchval(
            "SELECT count(*) FROM audit.audit_ledger "
            " WHERE id = $1::uuid "
            "   AND action_type = 'public_geoscience.pull.complete'",
            str(entry_id),
        )
        assert n == 1
