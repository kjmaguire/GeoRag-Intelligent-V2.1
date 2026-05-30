"""Live tests for doc-phase 154 SK + BC assessment_survey adapters."""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.publicgeo.assessment_survey_adapters import (
    _fetch_bc_aris_features,
    _fetch_sk_assessment_features,
    _square_footprint_wkt,
    sync_bc_aris_assessment_surveys,
    sync_sk_assessment_surveys,
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


def test_square_footprint_wkt_format():
    wkt = _square_footprint_wkt(50.0, -120.0, side_deg=0.04)
    assert wkt.startswith("MULTIPOLYGON(((")
    assert wkt.endswith(")))")


def test_sk_features_count_and_types():
    features = _fetch_sk_assessment_features()
    assert len(features) == 8
    types = {f.get("survey_type") for f in features}
    assert "airborne" in types
    assert "ground" in types


def test_bc_features_count_and_types():
    features = _fetch_bc_aris_features()
    assert len(features) == 8
    types = {f.get("survey_type") for f in features}
    assert "airborne" in types
    assert "ground" in types
    assert "underground" in types


@pytest.mark.asyncio
async def test_sync_sk_inserts_all_features(pool, conn):
    await conn.execute(
        "DELETE FROM public_geoscience.pg_assessment_survey "
        "WHERE source_id = 'sk_assessment_survey'"
    )
    result = await sync_sk_assessment_surveys(pool=pool)
    assert result.total_features == 8
    assert result.inserted == 8


@pytest.mark.asyncio
async def test_sync_bc_inserts_all_features(pool, conn):
    await conn.execute(
        "DELETE FROM public_geoscience.pg_assessment_survey "
        "WHERE source_id = 'bc_aris_assessment_survey'"
    )
    result = await sync_bc_aris_assessment_surveys(pool=pool)
    assert result.total_features == 8
    assert result.inserted == 8


@pytest.mark.asyncio
async def test_both_adapters_idempotent(pool):
    await sync_sk_assessment_surveys(pool=pool)
    await sync_bc_aris_assessment_surveys(pool=pool)
    r1 = await sync_sk_assessment_surveys(pool=pool)
    r2 = await sync_bc_aris_assessment_surveys(pool=pool)
    assert r1.inserted == 0 and r1.updated == 0
    assert r2.inserted == 0 and r2.updated == 0


@pytest.mark.asyncio
async def test_geom_persists_as_multipolygon(pool, conn):
    """Verify the geom column is populated and is a MultiPolygon."""
    await sync_sk_assessment_surveys(pool=pool)
    geom_type = await conn.fetchval(
        """
        SELECT GeometryType(geom) FROM public_geoscience.pg_assessment_survey
         WHERE source_id = 'sk_assessment_survey'
         LIMIT 1
        """
    )
    assert geom_type == "MULTIPOLYGON"
