"""§04e schema decisions (SME-approved, Kyle, 2026-09-29) against a real Postgres.

What the unit tests cannot show, because their FakeConns have no constraints,
no triggers and no indexes:

  * an up-hole collar is stored as measured and promote_silver_to_gold draws
    its trace RISING from the collar (decision 1);
  * a collar with no total depth is stored NULL — by the CSV collar writer
    too — and its straight-line trace reaches the deepest interval on record
    instead of being skipped (decision 2);
  * every collar writer's ``ON CONFLICT (project_id, hole_id_canonical)``
    lands a spelling variant on the existing collar, and the database
    derives ``hole_id_canonical`` whatever the writer sends (decision 3).
"""
from __future__ import annotations

import math
import os
import uuid
from typing import Any

import asyncpg
import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

from app.db import bind_workspace_scope  # noqa: E402
from app.db.dsn import build_dsn  # noqa: E402


@pytest.fixture
async def scope():
    conn = await asyncpg.connect(build_dsn(), statement_cache_size=0)
    ws = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    try:
        await conn.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1::uuid, $2, $3)",
            ws, "sme-decisions", f"sme-{ws[:8]}",
        )
        await bind_workspace_scope(conn, workspace_id=ws, is_local=False)
        await conn.execute(
            "INSERT INTO silver.projects (project_id, project_name, workspace_id, slug) "
            "VALUES ($1::uuid, $2, $3::uuid, $4)",
            project_id, "sme-decisions", ws, f"sme-{project_id[:8]}",
        )
        yield conn, ws, project_id
    finally:
        for table in ("silver.drill_traces", "silver.lithology_logs", "silver.surveys"):
            await conn.execute(
                f"DELETE FROM {table} WHERE collar_id IN "  # noqa: S608 — fixed table list
                "(SELECT collar_id FROM silver.collars WHERE project_id = $1::uuid)",
                project_id,
            )
        await conn.execute("DELETE FROM silver.collars WHERE project_id = $1::uuid", project_id)
        await conn.execute("DELETE FROM silver.projects WHERE project_id = $1::uuid", project_id)
        await conn.close()


async def _collar(
    conn: asyncpg.Connection, ws: str, project_id: str, hole_id: str, **cols: Any,
) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, project_id, workspace_id, easting, northing, elevation,
             total_depth, azimuth, dip, hole_type, status, geom_4326)
        VALUES (gen_random_uuid(), $1, $2::uuid, $3::uuid, 500000, 6000000, $4,
                $5, $6, $7, 'DD', 'active',
                ST_Transform(ST_SetSRID(ST_MakePoint(500000, 6000000), 32613), 4326))
        RETURNING collar_id::text
        """,
        hole_id, project_id, ws,
        cols.get("elevation", 1000.0), cols.get("total_depth"),
        cols.get("azimuth"), cols.get("dip"),
    )
    return str(row["collar_id"])


async def _trace_end_z(conn: asyncpg.Connection, collar_id: str) -> tuple[float, int]:
    row = await conn.fetchrow(
        "SELECT ST_Z(ST_EndPoint(geom)) AS z, ST_NPoints(geom) AS n "
        "FROM silver.drill_traces WHERE collar_id = $1::uuid",
        collar_id,
    )
    assert row is not None, "no trace was written"
    return float(row["z"]), int(row["n"])


async def _promote(conn: asyncpg.Connection, ws: str, project_id: str):
    from app.hatchet_workflows.promote_silver_to_gold import (
        PromoteSilverToGoldOutput,
        _promote_traces,
    )

    out = PromoteSilverToGoldOutput()
    await _promote_traces(conn, workspace_id=ws, project_id=project_id, out=out)
    return out


async def test_an_up_hole_is_stored_and_traced_upward(scope) -> None:
    conn, ws, project_id = scope
    collar_id = await _collar(
        conn, ws, project_id, "UG-UP-1", total_depth=40.0, azimuth=90.0, dip=30.0,
    )

    await _promote(conn, ws, project_id)

    z, _ = await _trace_end_z(conn, collar_id)
    assert z == pytest.approx(1000.0 + 40.0 * math.sin(math.radians(30.0)), abs=0.01)


async def test_a_collar_without_total_depth_traces_to_its_deepest_interval(scope) -> None:
    conn, ws, project_id = scope
    collar_id = await _collar(
        conn, ws, project_id, "NO-EOH-1", total_depth=None, azimuth=0.0, dip=-90.0,
    )
    await conn.execute(
        "INSERT INTO silver.lithology_logs (log_id, collar_id, workspace_id, from_depth, to_depth, lithology_code) "
        "VALUES (gen_random_uuid(), $1::uuid, $2::uuid, 0, 120, 'SST')",
        collar_id, ws,
    )

    out = await _promote(conn, ws, project_id)

    assert out.traces_skipped_no_geometry == 0
    z, _ = await _trace_end_z(conn, collar_id)
    assert z == pytest.approx(1000.0 - 120.0, abs=0.01)
    assert await conn.fetchval(
        "SELECT total_depth FROM silver.collars WHERE collar_id = $1::uuid", collar_id,
    ) is None, "the fallback is a reader's, the column stays NULL"


async def test_a_collar_with_nothing_to_measure_is_skipped_not_invented(scope) -> None:
    conn, ws, project_id = scope
    await _collar(conn, ws, project_id, "BARE-1", total_depth=None, azimuth=0.0, dip=-90.0)

    out = await _promote(conn, ws, project_id)

    assert out.traces_skipped_no_geometry == 1


async def test_the_database_derives_the_canonical_key(scope) -> None:
    conn, ws, project_id = scope
    await conn.execute(
        "INSERT INTO silver.collars (collar_id, hole_id, hole_id_canonical, project_id, workspace_id, "
        "easting, northing, hole_type, status) "
        "VALUES (gen_random_uuid(), ' leb-23/001 ', 'raw', $1::uuid, $2::uuid, 1, 2, 'DD', 'a')",
        project_id, ws,
    )
    assert await conn.fetchval(
        "SELECT hole_id_canonical FROM silver.collars WHERE project_id = $1::uuid", project_id,
    ) == "LEB23001"


async def test_csv_collar_writer_stores_null_and_lands_a_variant_on_the_collar(scope) -> None:
    from app.services.ingest.csv_collar_ingester import _upsert_collar

    conn, ws, project_id = scope
    base = {"easting": 500000.0, "northing": 6000000.0, "hole_type": "DD", "status": "active"}

    first = await _upsert_collar(
        conn, project_id=project_id, workspace_id=ws, crs_epsg=32613,
        record={**base, "hole_id": "SRE09_6", "total_depth": 150.0},
    )
    second = await _upsert_collar(
        conn, project_id=project_id, workspace_id=ws, crs_epsg=32613,
        record={**base, "hole_id": "SRE09-6", "total_depth": None},
    )
    third = await _upsert_collar(
        conn, project_id=project_id, workspace_id=ws, crs_epsg=32613,
        record={**base, "hole_id": "NEW-1", "total_depth": 0.0},
    )

    assert first == second, "a spelling variant updates the collar, it does not ghost it"
    rows = {
        r["hole_id"]: r["total_depth"] for r in await conn.fetch(
            "SELECT hole_id, total_depth FROM silver.collars WHERE project_id = $1::uuid",
            project_id,
        )
    }
    assert rows == {"SRE09_6": 150.0, "NEW-1": None}, (
        "the stored spelling and depth are kept; an absent or zero depth is NULL"
    )
    assert third != first
