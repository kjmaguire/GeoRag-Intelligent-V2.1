"""Integration tests against the live silver state populated by
Phase B-E.1 (doc-phase 183).

These tests verify the end-to-end ingestion landed correctly. They
read silver/Neo4j/Qdrant directly to assert state without re-running
the ingesters.

Skipped cleanly when the live data isn't present (e.g., fresh CI env
where Phase B hasn't run).
"""
from __future__ import annotations

import os

import asyncpg
import pytest


CAMECO_PROJECT_SLUG = "cameco-shirley-basin"


def _dsn() -> str:
    return (
        f"postgres://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:5432/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )


# Tenant Isolation Block 1 (2026-05-15) migrated silver.collars + reports +
# well_log_curves to strict workspace_id RLS. The Cameco fixture rows now
# live in the Default Workspace; tests must set the GUC to see them.
_DEFAULT_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


@pytest.fixture
async def pg_conn():
    """Direct asyncpg connection (bypasses pgbouncer for GUC stability).

    Sets ``app.workspace_id`` to the Default Workspace so the Cameco
    fixture rows are visible under the strict post-Block-1 RLS policies.
    """
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)",
            _DEFAULT_WORKSPACE_ID,
        )
        yield conn
    finally:
        await conn.close()


async def _cameco_project_id(conn: asyncpg.Connection) -> str | None:
    row = await conn.fetchrow(
        "SELECT project_id::text FROM silver.projects WHERE slug = $1",
        CAMECO_PROJECT_SLUG,
    )
    return row["project_id"] if row else None


# ─────────────────── silver state — Phase B ───────────────────────

@pytest.mark.asyncio
async def test_cameco_project_exists(pg_conn):
    """Phase B-Tier-1 created the Cameco Shirley Basin Uranium project."""
    pid = await _cameco_project_id(pg_conn)
    if pid is None:
        pytest.skip("Cameco project not yet ingested — skipping integration test")
    row = await pg_conn.fetchrow(
        "SELECT project_name, commodity, region "
        "FROM silver.projects WHERE project_id = $1::uuid",
        pid,
    )
    assert "CAMECO" in row["project_name"].upper() or "Cameco" in row["project_name"]
    assert row["commodity"] == "uranium"
    assert "WY" in (row["region"] or "")


@pytest.mark.asyncio
async def test_cameco_drillholes_present(pg_conn):
    """At least 60 drillholes (63 ingested doc-phase 179)."""
    pid = await _cameco_project_id(pg_conn)
    if pid is None:
        pytest.skip("Cameco project not yet ingested")
    count = await pg_conn.fetchval(
        "SELECT count(*) FROM silver.collars WHERE project_id = $1::uuid",
        pid,
    )
    assert count >= 60, f"Expected >=60 Cameco collars, got {count}"


@pytest.mark.asyncio
async def test_cameco_well_log_curves_include_gamma_grade(pg_conn):
    """Each Cameco hole has GAMMA + GRADE curves (uranium proxies)."""
    pid = await _cameco_project_id(pg_conn)
    if pid is None:
        pytest.skip("Cameco project not yet ingested")
    rows = await pg_conn.fetch(
        """
        SELECT wc.curve_name, count(*) AS n
          FROM silver.well_log_curves wc
          JOIN silver.collars c ON wc.collar_id = c.collar_id
         WHERE c.project_id = $1::uuid
           AND wc.curve_name IN ('GAMMA', 'GRADE')
         GROUP BY wc.curve_name
        """,
        pid,
    )
    curves = {r["curve_name"]: r["n"] for r in rows}
    assert curves.get("GAMMA", 0) >= 60, f"Expected >=60 GAMMA curves, got {curves}"
    assert curves.get("GRADE", 0) >= 60, f"Expected >=60 GRADE curves, got {curves}"


@pytest.mark.asyncio
async def test_cameco_holes_geographically_clustered(pg_conn):
    """All Cameco holes should be in Wyoming (lat 41-46°N, lon -111 to -104°W).

    This guards against coord-transform regressions that mis-project
    holes outside the state.
    """
    pid = await _cameco_project_id(pg_conn)
    if pid is None:
        pytest.skip("Cameco project not yet ingested")
    bad = await pg_conn.fetchval(
        """
        SELECT count(*) FROM silver.collars
         WHERE project_id = $1::uuid
           AND (ST_X(geom_4326) > -104 OR ST_X(geom_4326) < -111
                OR ST_Y(geom_4326) < 41 OR ST_Y(geom_4326) > 46)
        """,
        pid,
    )
    assert bad == 0, f"{bad} Cameco holes have coords outside Wyoming bounds"


# ─────────────────── silver state — Phase D ───────────────────────

@pytest.mark.asyncio
async def test_cameco_document_passages_present(pg_conn):
    """At least the 3 PDF passages from doc-phase 179 are present.

    Post-OCR (doc-phase 182) we expect ~1,100+ from TIFFs.
    """
    pid = await _cameco_project_id(pg_conn)
    if pid is None:
        pytest.skip("Cameco project not yet ingested")
    count = await pg_conn.fetchval(
        """
        SELECT count(*) FROM silver.document_passages dp
          JOIN silver.reports r ON dp.document_id = r.report_id
         WHERE r.project_id = $1::uuid
        """,
        pid,
    )
    assert count >= 3, f"Expected >=3 Cameco passages, got {count}"


# ───────────────────── Provenance trail ───────────────────────────

@pytest.mark.asyncio
async def test_bronze_provenance_links_silver_records(pg_conn):
    """Every Cameco collar should have a bronze.provenance row tying it
    to its source LAS file."""
    pid = await _cameco_project_id(pg_conn)
    if pid is None:
        pytest.skip("Cameco project not yet ingested")
    untraced = await pg_conn.fetchval(
        """
        SELECT count(*) FROM silver.collars c
         WHERE c.project_id = $1::uuid
           AND NOT EXISTS (
               SELECT 1 FROM bronze.provenance p
                WHERE p.target_table = 'collars'
                  AND p.target_id = c.collar_id
           )
        """,
        pid,
    )
    # Allow up to 5 untraced (3 LAS-skipped + buffer for partial runs)
    assert untraced <= 5, (
        f"{untraced} Cameco collars lack bronze.provenance — audit chain broken"
    )


# ─────────────────── core_chat question set ───────────────────────

@pytest.mark.asyncio
async def test_core_chat_wyoming_questions_seeded(pg_conn):
    """10 SME-drafted Wyoming uranium core_chat questions land active."""
    count = await pg_conn.fetchval(
        "SELECT count(*) FROM eval.golden_questions "
        "WHERE question_set = 'core_chat' AND status = 'active'"
    )
    assert count >= 10, f"Expected >=10 core_chat questions, got {count}"
