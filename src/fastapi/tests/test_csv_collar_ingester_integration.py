"""Live-Postgres integration tests for the CSV collar ingester.

Exercises the real dispatch path — `ingest_zip_archive._ingest_one` with
ext="csv" — end to end against a live database, asserting rows land in
`silver.collars` with correct values and workspace/project scoping.

Split into its own module (rather than living alongside the pure-Python
unit tests in test_csv_collar_ingester.py) because
`pytest.skip(..., allow_module_level=True)` aborts import of the *whole*
module it's called in — mixing it into a file that also defines
always-run unit tests would silently skip those too. This mirrors the
existing convention in tests/test_ingest_progress_state_machine.py:
skip-safe module-level guard on POSTGRES_USER, so this is a no-op
outside a live docker-compose stack and a real assertion inside one
(e.g. run from inside the `georag-fastapi` container, or any
environment with POSTGRES_* exported).
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

import pathlib  # noqa: E402
import uuid  # noqa: E402

import asyncpg  # noqa: E402

from app.hatchet_workflows.ingest_zip_archive import IngestZipArchiveInput, _ingest_one  # noqa: E402

_SAMPLE_CSV = pathlib.Path(__file__).parent / "fixtures" / "csv_collar" / "collars_sample.csv"

_TEST_WORKSPACE = "c2000000-0000-0000-0000-0000000000c2"
_TEST_PROJECT = "d3000000-0000-0000-0000-0000000000d3"


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _ensure_test_workspace_and_project() -> None:
    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO silver.workspaces (workspace_id, name, slug)
            VALUES ($1::uuid, 'csv-collar-ingester-tests',
                    'csv-collar-ingester-tests-' || substring($1::text from 1 for 8))
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            _TEST_WORKSPACE,
        )
        await conn.execute(
            """
            INSERT INTO silver.projects (
                project_id, project_name, slug, workspace_id,
                crs_datum, crs_epsg, orientation_reference, status
            ) VALUES (
                $1::uuid, 'csv-collar-ingester-tests',
                'csv-collar-ingester-tests-' || substring($1::text from 1 for 8),
                $2::uuid, 'EPSG:32613', 32613, 'grid_north', 'active'
            )
            ON CONFLICT (project_id) DO NOTHING
            """,
            _TEST_PROJECT, _TEST_WORKSPACE,
        )
    finally:
        await conn.close()


async def _cleanup_collars(hole_ids: list[str]) -> None:
    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "DELETE FROM silver.collars WHERE project_id = $1::uuid AND hole_id = ANY($2::text[])",
            _TEST_PROJECT, hole_ids,
        )
    finally:
        await conn.close()


@pytest.fixture
async def db_conn():
    await _ensure_test_workspace_and_project()
    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    await conn.execute("SELECT set_config('app.workspace_id', $1, false)", _TEST_WORKSPACE)
    await conn.execute("SELECT set_config('app.project_id', $1, false)", _TEST_PROJECT)
    try:
        yield conn
    finally:
        await conn.close()
        await _cleanup_collars(["CSVT-001", "CSVT-002", "CSVT-003", "CSVT-004"])


async def test_ingest_one_csv_branch_lands_rows_in_silver_collars(db_conn):
    """End-to-end: _ingest_one(ext="csv") on the fixture CSV must land
    exactly the two valid rows in silver.collars, correctly scoped to the
    test workspace/project, with the two invalid rows (missing easting;
    out-of-range dip) skipped rather than aborting the file.
    """
    fake_input = IngestZipArchiveInput(
        minio_key="archive/test.zip",
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        run_id=str(uuid.uuid4()),
    )
    counts: dict[str, int] = {"csv": 0, "skipped": 0, "errors": 0, "unknown": 0}

    await _ingest_one(
        file_path=_SAMPLE_CSV,
        ext="csv",
        conn=db_conn,
        store=None,
        input=fake_input,
        counts=counts,
    )

    assert counts["csv"] == 1
    assert counts["skipped"] == 0  # file-level: 2/4 rows landed, so not a whole-file skip

    rows = await db_conn.fetch(
        """
        SELECT hole_id, hole_id_canonical, easting, northing, elevation,
               total_depth, azimuth, dip, workspace_id::text AS workspace_id,
               project_id::text AS project_id, georef_method
          FROM silver.collars
         WHERE project_id = $1::uuid AND hole_id = ANY($2::text[])
         ORDER BY hole_id
        """,
        _TEST_PROJECT, ["CSVT-001", "CSVT-002", "CSVT-003", "CSVT-004"],
    )

    # Only the two valid rows landed — CSVT-003 (blank easting) and
    # CSVT-004 (dip=-999, out of range) were skipped, not aborted-into.
    landed_ids = {r["hole_id"] for r in rows}
    assert landed_ids == {"CSVT-001", "CSVT-002"}

    row1 = next(r for r in rows if r["hole_id"] == "CSVT-001")
    assert row1["hole_id_canonical"] == "CSVT001"
    assert row1["easting"] == pytest.approx(471250.5)
    assert row1["northing"] == pytest.approx(4657100.0)
    assert row1["elevation"] == pytest.approx(1830.2)
    assert row1["total_depth"] == pytest.approx(152.4)
    assert row1["azimuth"] == pytest.approx(45.0)
    assert row1["dip"] == pytest.approx(-60.0)
    assert row1["workspace_id"] == _TEST_WORKSPACE
    assert row1["project_id"] == _TEST_PROJECT
    assert row1["georef_method"] == "declared"

    row2 = next(r for r in rows if r["hole_id"] == "CSVT-002")
    assert row2["total_depth"] == pytest.approx(88.0)


async def test_ingest_one_csv_branch_is_idempotent_on_rerun(db_conn):
    """Re-running the same file (e.g. a Hatchet retry) must UPSERT, not
    duplicate rows — same (project_id, hole_id) conflict target the
    LAS/Cameco branches use."""
    fake_input = IngestZipArchiveInput(
        minio_key="archive/test.zip",
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        run_id=str(uuid.uuid4()),
    )
    counts: dict[str, int] = {"csv": 0, "skipped": 0, "errors": 0, "unknown": 0}

    for _ in range(2):
        await _ingest_one(
            file_path=_SAMPLE_CSV, ext="csv", conn=db_conn, store=None,
            input=fake_input, counts=counts,
        )

    count_row = await db_conn.fetchrow(
        "SELECT count(*) AS n FROM silver.collars WHERE project_id = $1::uuid AND hole_id = 'CSVT-001'",
        _TEST_PROJECT,
    )
    assert count_row["n"] == 1
