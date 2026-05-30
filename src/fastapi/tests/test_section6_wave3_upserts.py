"""§6.2 wave 3 — UPSERT branch tests for the 4 new canonical tables.

Confirms each new branch in `_upsert_features` is end-to-end
correct: feature in → row out, second call updates rather than
duplicates, geometries parse, status/survey-type normalisation
applied, idempotent on re-run.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.hatchet_workflows.bc_minfile_pull import _upsert_features

PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN, statement_cache_size=0)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def synthetic_source(pg_conn: asyncpg.Connection):
    """Provision a throwaway source row so we can INSERT against it
    without colliding with the real registered sources.
    """
    source_id = f"test_wave3_{uuid.uuid4().hex[:8]}"
    await pg_conn.execute(
        """
        INSERT INTO public_geoscience.sources (
            source_id, jurisdiction_code, name, canonical_type,
            service_url, license_url, license_summary
        )
        VALUES ($1, 'CA-BC', 'wave3 test', 'mineral_occurrence',
                'https://example.invalid/arcgis', 'https://test', 'test')
        """,
        source_id,
    )
    yield {
        "source_id": source_id,
        "jurisdiction_code": "CA-BC",
        "service_url": "https://example.invalid/arcgis",
    }
    # Cleanup
    for tbl in (
        "pg_drillhole_collar", "pg_mine",
        "pg_bedrock_geology", "pg_assessment_survey",
    ):
        await pg_conn.execute(
            f"DELETE FROM public_geoscience.{tbl} WHERE source_id = $1",
            source_id,
        )
    await pg_conn.execute(
        "DELETE FROM public_geoscience.sources WHERE source_id = $1",
        source_id,
    )


# ---------------------------------------------------------------------------
# pg_drillhole_collar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drillhole_collar_insert_and_update(
    pg_conn: asyncpg.Connection, synthetic_source: dict,
):
    feature = {
        "properties": {
            "HOLE_ID": "WAVE3-001",
            "NAME": "Test Hole A",
            "COMPANY": "TestCo",
            "PROJECT": "ProjectX",
            "DEPTH": "350.5",
            "DIP": "-65.0",
            "AZIMUTH": "180.0",
            "ELEVATION": "1250.0",
            "COMMODITY": "Au, Cu",
        },
        "geometry": {"type": "Point", "coordinates": [-127.5, 54.2]},
    }
    n = await _upsert_features(
        pg_conn, "public_geoscience.pg_drillhole_collar",
        synthetic_source, [feature],
    )
    assert n == 1

    row = await pg_conn.fetchrow(
        """
        SELECT drillhole_id, drillhole_name, total_length_m,
               inclination_deg, azimuth_deg, collar_elevation_m,
               commodity_of_interest
          FROM public_geoscience.pg_drillhole_collar
         WHERE source_id = $1 AND source_feature_id = 'WAVE3-001'
        """,
        synthetic_source["source_id"],
    )
    assert row is not None
    assert row["drillhole_id"] == "WAVE3-001"
    assert row["drillhole_name"] == "Test Hole A"
    assert float(row["total_length_m"]) == 350.5
    assert float(row["inclination_deg"]) == -65.0
    assert float(row["azimuth_deg"]) == 180.0
    assert float(row["collar_elevation_m"]) == 1250.0
    assert sorted(row["commodity_of_interest"]) == ["Au", "Cu"]

    # Re-upsert with changed depth — should UPDATE, not duplicate
    feature["properties"]["DEPTH"] = "400.0"
    n2 = await _upsert_features(
        pg_conn, "public_geoscience.pg_drillhole_collar",
        synthetic_source, [feature],
    )
    assert n2 == 1
    count = await pg_conn.fetchval(
        "SELECT count(*) FROM public_geoscience.pg_drillhole_collar WHERE source_id = $1",
        synthetic_source["source_id"],
    )
    assert count == 1
    updated_depth = await pg_conn.fetchval(
        """
        SELECT total_length_m FROM public_geoscience.pg_drillhole_collar
         WHERE source_id = $1 AND source_feature_id = 'WAVE3-001'
        """,
        synthetic_source["source_id"],
    )
    assert float(updated_depth) == 400.0


# ---------------------------------------------------------------------------
# pg_mine
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mine_status_normalization(
    pg_conn: asyncpg.Connection, synthetic_source: dict,
):
    feature_1 = {
        "properties": {
            "MINE_ID": "MINE-001",
            "NAME": "Test Mine North",
            "MINE_STATUS": "Past Producer",  # alias → 'historic'
            "COMMODITIES": "Cu|Au",
            "OPERATOR": "MiningCo",
        },
        "geometry": {"type": "Point", "coordinates": [-127.5, 54.2]},
    }
    feature_2 = {
        "properties": {
            "MINE_ID": "MINE-002",
            "NAME": "Test Mine South",
            "MINE_STATUS": "In Operation",  # alias → 'operating'
            "COMMODITIES": "Mo",
        },
        "geometry": {"type": "Point", "coordinates": [-127.3, 54.1]},
    }
    n = await _upsert_features(
        pg_conn, "public_geoscience.pg_mine",
        synthetic_source, [feature_1, feature_2],
    )
    assert n == 2

    rows = await pg_conn.fetch(
        """
        SELECT source_feature_id, name, status, commodities, operator
          FROM public_geoscience.pg_mine
         WHERE source_id = $1 ORDER BY source_feature_id
        """,
        synthetic_source["source_id"],
    )
    assert len(rows) == 2
    statuses = {r["source_feature_id"]: r["status"] for r in rows}
    # Past Producer is an alias for the canonical past-producer
    assert statuses["MINE-001"] == "past-producer"
    # In Operation is an alias for the canonical producing
    assert statuses["MINE-002"] == "producing"

    by_id = {r["source_feature_id"]: r for r in rows}
    assert sorted(by_id["MINE-001"]["commodities"]) == ["Au", "Cu"]


# ---------------------------------------------------------------------------
# pg_bedrock_geology
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bedrock_geology_polygon_insert(
    pg_conn: asyncpg.Connection, synthetic_source: dict,
):
    feature = {
        "properties": {
            "UNIT_CODE": "JKgr",
            "UNIT_NAME": "Granite of the Jurassic",
            "EON": "Phanerozoic",
            "ERA": "Mesozoic",
            "PERIOD": "Jurassic",
            "GROUP": "Coast Plutonic",
            "FORMATION": None,
            "LITHOLOGY": "granite",
            "SCALE": "250K",
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-128.0, 54.0], [-127.0, 54.0],
                [-127.0, 55.0], [-128.0, 55.0],
                [-128.0, 54.0],
            ]],
        },
    }
    n = await _upsert_features(
        pg_conn, "public_geoscience.pg_bedrock_geology",
        synthetic_source, [feature],
    )
    assert n == 1

    row = await pg_conn.fetchrow(
        """
        SELECT unit_code, unit_name, era, period, lithology,
               ST_GeometryType(geom) AS gt
          FROM public_geoscience.pg_bedrock_geology
         WHERE source_id = $1 AND source_feature_id = 'JKgr'
        """,
        synthetic_source["source_id"],
    )
    assert row is not None
    assert row["unit_code"] == "JKgr"
    assert row["era"] == "Mesozoic"
    assert row["period"] == "Jurassic"
    # ST_Multi wrapping in the UPSERT means single Polygon → MultiPolygon
    assert row["gt"] == "ST_MultiPolygon"


# ---------------------------------------------------------------------------
# pg_assessment_survey
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_assessment_survey_type_normalization(
    pg_conn: asyncpg.Connection, synthetic_source: dict,
):
    features = [
        {
            "properties": {
                "SURVEY_ID": "ASR-001",
                "SURVEY_TYPE": "Aeromagnetic",  # alias → airborne
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-127.0, 54.0], [-126.0, 54.0],
                    [-126.0, 55.0], [-127.0, 55.0],
                    [-127.0, 54.0],
                ]],
            },
        },
        {
            "properties": {
                "SURVEY_ID": "ASR-002",
                "SURVEY_TYPE": "borehole",  # alias → underground
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-127.5, 54.5], [-127.0, 54.5],
                    [-127.0, 55.0], [-127.5, 55.0],
                    [-127.5, 54.5],
                ]],
            },
        },
        {
            "properties": {
                "SURVEY_ID": "ASR-003",
                "SURVEY_TYPE": None,  # → unknown
            },
            "geometry": None,
        },
    ]
    n = await _upsert_features(
        pg_conn, "public_geoscience.pg_assessment_survey",
        synthetic_source, features,
    )
    assert n == 3

    rows = await pg_conn.fetch(
        """
        SELECT source_feature_id, survey_type
          FROM public_geoscience.pg_assessment_survey
         WHERE source_id = $1 ORDER BY source_feature_id
        """,
        synthetic_source["source_id"],
    )
    by_id = {r["source_feature_id"]: r["survey_type"] for r in rows}
    assert by_id["ASR-001"] == "airborne"
    assert by_id["ASR-002"] == "underground"
    assert by_id["ASR-003"] == "unknown"


# ---------------------------------------------------------------------------
# Unmapped target is a graceful no-op
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unmapped_target_returns_zero(
    pg_conn: asyncpg.Connection, synthetic_source: dict,
):
    n = await _upsert_features(
        pg_conn, "public_geoscience.pg_unknown_table",
        synthetic_source, [{"properties": {"x": 1}, "geometry": None}],
    )
    assert n == 0
