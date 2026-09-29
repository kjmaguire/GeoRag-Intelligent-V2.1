"""The tabular writers against a real Postgres: one bad row never fails a file.

WHY THIS FILE EXISTS (audit 2026-09-29, ING-1 / ING-2 / ING-14)
    Every claim below was reproduced against a scratch Postgres with the real
    ``chk_*`` constraints; the writer tests use a recording fake that has no
    constraints, so none of it could fail in CI.

      * ING-1: a collar file with no total-depth column raised
        CheckViolationError (the writer defaulted the depth to 0.0); a
        mine-grid RL of 9,650 or an up-hole dip of +60 did the same; a
        700-row file with ONE bad row committed 500 collars and then raised.
      * ING-2: a second sample sheet for the same holes in the same run
        deleted the first sheet's samples and assays.
      * ING-14: a separator variant of an existing hole id created a second
        "ghost" collar.

    The guard's bounds are also compared with the LIVE constraint definitions
    here (test_silver_row_guard.py compares them with the migration files).

WHAT THIS DOES NOT COVER
    The Hatchet task wrapper, bronze storage, or progress reporting.
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any

import asyncpg
import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

from app.db import bind_workspace_scope  # noqa: E402
from app.db.dsn import build_dsn  # noqa: E402
from app.hatchet_workflows.ingest_tabular import (  # noqa: E402
    _collar_index,
    _write_collars,
    _write_intervals,
)
from app.services.ingest import silver_row_guard as guard  # noqa: E402
from app.services.ingest.silver_row_guard import RowIssues  # noqa: E402


class _Fixture:
    def __init__(self, conn: asyncpg.Connection, ws: str, project: str) -> None:
        self.conn = conn
        self.workspace_id = ws
        self.project_id = project

    async def collars(self, records: list[dict[str, Any]], issues: RowIssues | None = None) -> dict:
        return await _write_collars(
            self.conn, workspace_id=self.workspace_id, project_id=self.project_id,
            records=records, epsg=32613, georef_method="declared", issues=issues,
        )

    async def count(self, sql: str) -> int:
        return int(await self.conn.fetchval(sql, self.project_id) or 0)


@pytest.fixture
async def project():
    conn = await asyncpg.connect(build_dsn(), statement_cache_size=0)
    ws = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    try:
        await conn.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug) "
            "VALUES ($1::uuid, $2, $3) ON CONFLICT DO NOTHING",
            ws, "constraint-rows", f"constraint-rows-{ws[:8]}",
        )
        await bind_workspace_scope(conn, workspace_id=ws, is_local=False)
        await conn.execute(
            "INSERT INTO silver.projects (project_id, project_name, workspace_id, "
            "  crs_datum, orientation_reference, slug) "
            "VALUES ($1::uuid, $2, $3::uuid, 'EPSG:32613', 'BOH', $4) "
            "ON CONFLICT DO NOTHING",
            project_id, "constraint-rows", ws, f"constraint-rows-{project_id[:8]}",
        )
        yield _Fixture(conn, ws, project_id)
    finally:
        for table in ("silver.assays_v2", "silver.samples", "silver.lithology_logs"):
            await conn.execute(
                f"DELETE FROM {table} WHERE collar_id IN "  # noqa: S608
                "(SELECT collar_id FROM silver.collars WHERE project_id = $1::uuid)",
                project_id,
            )
        await conn.execute("DELETE FROM silver.collars WHERE project_id = $1::uuid", project_id)
        await conn.execute("DELETE FROM silver.projects WHERE project_id = $1::uuid", project_id)
        await conn.close()


_N_COLLARS = "SELECT count(*) FROM silver.collars WHERE project_id = $1::uuid"


def _collar(hole: str, row: int, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "hole_id": hole, "easting": 500000.0 + row, "northing": 6000000.0,
        "total_depth": 100.0, "_source_row": row,
    }
    rec.update(extra)
    return rec


async def test_guard_matches_live_constraints(project: _Fixture) -> None:
    rows = await project.conn.fetch(
        "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conname = ANY($1::text[])",
        ["chk_elevation_range", "chk_azimuth_range", "chk_dip_range",
         "chk_rqd_range", "chk_recovery_range", "chk_total_depth_positive"],
    )
    live = {r["conname"]: [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", r["def"].replace("'", ""))]
            for r in rows}
    assert tuple(live["chk_elevation_range"]) == guard.COLLAR_ELEVATION_RANGE
    assert tuple(live["chk_azimuth_range"]) == guard.COLLAR_AZIMUTH_RANGE
    assert tuple(live["chk_dip_range"]) == guard.COLLAR_DIP_RANGE
    assert tuple(live["chk_rqd_range"]) == guard.LITHOLOGY_PERCENT_RANGE
    assert tuple(live["chk_recovery_range"]) == guard.LITHOLOGY_PERCENT_RANGE
    assert live["chk_total_depth_positive"] == [guard.COLLAR_TOTAL_DEPTH_EXCLUSIVE_MIN]


async def test_no_total_depth_skips_rows_and_does_not_raise(project: _Fixture) -> None:
    issues = RowIssues()
    stats = await project.collars(
        [_collar("A-1", 2, total_depth=None), _collar("A-2", 3)], issues,
    )
    assert stats == {"written": 1, "skipped": 1, "orphaned": 0}
    assert await project.count(_N_COLLARS) == 1
    assert issues.skipped[0][:2] == (2, "A-1")


async def test_out_of_range_values_are_blanked_and_collars_kept(project: _Fixture) -> None:
    issues = RowIssues()
    stats = await project.collars([
        _collar("RL-1", 2, elevation=9650.0),        # mine grid RL
        _collar("UP-1", 3, dip=60.0),                # up-hole
        _collar("AZ-1", 4, azimuth=400.0),
    ], issues)
    assert stats["written"] == 3
    rows = {
        r["hole_id"]: r for r in await project.conn.fetch(
            "SELECT hole_id, elevation, dip, azimuth FROM silver.collars "
            "WHERE project_id = $1::uuid", project.project_id,
        )
    }
    assert rows["RL-1"]["elevation"] is None
    assert rows["UP-1"]["dip"] is None
    assert rows["AZ-1"]["azimuth"] is None
    assert sorted(b[0] for b in issues.blanked) == ["azimuth", "dip", "elevation"]


async def test_one_bad_row_in_700_writes_the_other_699(project: _Fixture) -> None:
    records = [_collar(f"H-{i:04d}", i + 2) for i in range(700)]
    records[650]["total_depth"] = None
    stats = await project.collars(records)
    assert stats["written"] == 699 and stats["skipped"] == 1
    assert await project.count(_N_COLLARS) == 699


async def test_a_batch_failure_leaves_nothing_half_written(project: _Fixture) -> None:
    """All batches are one transaction: batch 2 failing rolls back batch 1."""
    records = [_collar(f"T-{i:04d}", i + 2) for i in range(600)]
    # Not something the guard judges: an unencodable date fails asyncpg's
    # encoder inside the SECOND executemany batch.
    records[550]["drill_date"] = "not-a-date"
    with pytest.raises((asyncpg.DataError, asyncpg.PostgresError, TypeError, ValueError)):
        await project.collars(records)
    assert await project.count(_N_COLLARS) == 0


async def test_separator_variant_updates_the_existing_collar(project: _Fixture) -> None:
    await project.collars([_collar("SRE09_6", 2, total_depth=150.0)])
    issues = RowIssues()
    await project.collars([_collar("SRE09-6", 2, total_depth=None, easting=500123.0)], issues)
    rows = await project.conn.fetch(
        "SELECT hole_id, easting, total_depth FROM silver.collars "
        "WHERE project_id = $1::uuid", project.project_id,
    )
    assert [(r["hole_id"], r["easting"], r["total_depth"]) for r in rows] == [
        ("SRE09_6", 500123.0, 150.0),
    ]
    assert issues.merged == [(2, "SRE09-6", "SRE09_6")]


async def test_overlong_hole_type_keeps_collar_and_text(project: _Fixture) -> None:
    text = "Diamond core, HQ to 120 m then NQ to EOH"
    stats = await project.collars([_collar("LT-1", 2, hole_type=text, status="x" * 30)])
    assert stats["written"] == 1
    row = await project.conn.fetchrow(
        "SELECT hole_type, drill_type, status, hole_status FROM silver.collars "
        "WHERE project_id = $1::uuid", project.project_id,
    )
    assert (row["hole_type"], row["drill_type"]) == ("unknown", text)
    assert (row["status"], row["hole_status"]) == ("unknown", "x" * 30)


def _sample(hole: str, frm: float, to: float, sample_id: str, assays: dict[str, float]) -> dict[str, Any]:
    return {
        "hole_id": hole, "from_depth": frm, "to_depth": to, "sample_id": sample_id,
        "sample_type": "Core", "commodity_assays": assays, "_source_row": 2,
    }


async def test_second_sheet_in_one_run_appends_and_next_run_replaces(project: _Fixture) -> None:
    await project.collars([_collar("DH-001", 2)])
    index = await _collar_index(project.conn, project.project_id)
    scope: set[tuple[str, str]] = set()
    sheet_a = [_sample("DH-001", 0, 1, "S1", {"Au_ppm": 0.5}),
               _sample("DH-001", 1, 2, "S2", {"Au_ppm": 0.7})]
    sheet_b = [_sample("DH-001", 0, 1, "S1", {"Cu_pct": 0.1}),
               _sample("DH-001", 1, 2, "S2", {"Cu_pct": 0.3})]
    for sheet in (sheet_a, sheet_b):
        await _write_intervals(
            project.conn, workspace_id=project.workspace_id, sheet_type="sample",
            records=sheet, index=index, replaced_scope=scope,
        )
    cid = index["DH-001"]
    elements = await project.conn.fetch(
        "SELECT element, count(*) AS n FROM silver.assays_v2 WHERE collar_id = $1::uuid "
        "GROUP BY 1 ORDER BY 1", cid,
    )
    assert [(r["element"], r["n"]) for r in elements] == [("Au", 2), ("Cu", 2)]

    # A NEW run (fresh scope) re-uploading sheet A replaces the hole's rows.
    stats = await _write_intervals(
        project.conn, workspace_id=project.workspace_id, sheet_type="sample",
        records=sheet_a, index=index, replaced_scope=set(),
    )
    assert stats["replaced"] == 4
    elements = await project.conn.fetch(
        "SELECT DISTINCT element FROM silver.assays_v2 WHERE collar_id = $1::uuid", cid,
    )
    assert [r["element"] for r in elements] == ["Au"]


async def test_lithology_out_of_range_recovery_is_blanked_not_fatal(project: _Fixture) -> None:
    await project.collars([_collar("L-1", 2)])
    index = await _collar_index(project.conn, project.project_id)
    issues = RowIssues()
    stats = await _write_intervals(
        project.conn, workspace_id=project.workspace_id, sheet_type="lithology",
        records=[
            {"hole_id": "L-1", "from_depth": 0, "to_depth": 3, "lithology_code": "SST",
             "rqd": 85, "recovery": 102, "_source_row": 2},
            {"hole_id": "L-1", "from_depth": 3, "to_depth": 6,
             "lithology_code": "Very long free-text lithology name", "_source_row": 3},
        ],
        index=index, issues=issues,
    )
    assert stats["written"] == 2
    rows = await project.conn.fetch(
        "SELECT lithology_code, lithology_description, rqd, recovery FROM silver.lithology_logs "
        "WHERE collar_id = $1::uuid ORDER BY from_depth", index["L-1"],
    )
    assert (rows[0]["rqd"], rows[0]["recovery"]) == (85.0, None)
    assert rows[1]["lithology_code"] is None
    assert rows[1]["lithology_description"] == "Very long free-text lithology name"
