"""The collar upsert and the assay writer against a real Postgres (audit findings 1, 3, 12).

WHY THIS FILE EXISTS
    ``_COLLAR_SQL`` is ``ON CONFLICT (project_id, hole_id_canonical) DO UPDATE``,
    and what that does to a stored collar depends on the table's triggers
    (``derive_collar_spatial_uncertainty``), so a recording fake cannot judge it.

      * Finding 3: a later collar file that merely lacked an elevation /
        azimuth / dip / drill_date column erased the stored values with NULL
        and wrote hole_type / status over them with 'unknown'; a hole
        re-uploaded under a different CRS decision kept its old
        ``georef_method`` and spatial uncertainty; and ``crs_confidence`` was
        stamped by the FILE's spelling of the hole id, which misses a collar
        the writer had landed on under its stored spelling.
      * Findings 1 and 12: an assay row with no sample id reaches
        ``silver.assays_v2`` under a derived id, and ``qaqc_flag`` is written
        explicitly (NULL or a control marker), never the column default.
      * Finding 21: ``silver.geochemistry`` rows record the file, hash and row
        position that wrote them, and a sample number taken over from another
        file is reported.

    Needs the migrated ``silver`` schema; skipped without POSTGRES_USER, like
    its sibling test_ingest_constraint_rows_integration.py.
"""
from __future__ import annotations

# ruff: noqa: F811 - the `project` fixture is imported, then named as a parameter
import datetime
from typing import Any

import pytest

from app.hatchet_workflows.ingest_tabular import (
    _collar_index,
    _stamp_crs_confidence,
    _write_collars,
    _write_intervals,
)
from app.services.ingest.silver_row_guard import RowIssues

# Importing the sibling module also applies its module-level skip when no
# Postgres is configured, and brings the `project` fixture along.
from tests.test_ingest_constraint_rows_integration import (  # noqa: F401 - `project` is a fixture
    _collar,
    _Fixture,
    project,
)

pytestmark = pytest.mark.integration

_COLLAR_ROW = (
    "SELECT hole_id, elevation, azimuth, dip, drill_date, hole_type, status, total_depth, "
    "       georef_method, spatial_uncertainty_m, spatial_uncertainty_method, crs_confidence "
    "FROM silver.collars WHERE project_id = $1::uuid"
)


async def _write(project: _Fixture, records: list[dict[str, Any]], method: str = "declared") -> dict:
    return await _write_collars(
        project.conn, workspace_id=project.workspace_id, project_id=project.project_id,
        records=records, epsg=32613, georef_method=method,
    )


async def _one(project: _Fixture) -> Any:
    rows = await project.conn.fetch(_COLLAR_ROW, project.project_id)
    assert len(rows) == 1
    return rows[0]


FULL = {
    "elevation": 350.0, "azimuth": 90.0, "dip": -60.0, "hole_type": "DD",
    "status": "completed", "drill_date": datetime.date(2019, 5, 1),
}


async def test_a_later_file_without_attributes_keeps_the_stored_ones(project: _Fixture) -> None:
    await _write(project, [_collar("A-1", 2, **FULL)])
    # A second file for the same hole: coordinates and depth only.
    await _write(project, [_collar("A-1", 2, total_depth=150.0, easting=500050.0)])

    row = await _one(project)
    assert (row["elevation"], row["azimuth"], row["dip"]) == (350.0, 90.0, -60.0)
    assert row["drill_date"] == datetime.date(2019, 5, 1)
    assert (row["hole_type"], row["status"]) == ("DD", "completed")   # not 'unknown'
    assert row["total_depth"] == 150.0


async def test_a_file_that_carries_a_value_replaces_it(project: _Fixture) -> None:
    await _write(project, [_collar("A-1", 2, **FULL)])
    await _write(project, [_collar(
        "A-1", 2, elevation=360.0, azimuth=100.0, dip=-70.0, hole_type="RC",
        status="abandoned", drill_date=datetime.date(2020, 1, 2),
    )])

    row = await _one(project)
    assert (row["elevation"], row["azimuth"], row["dip"]) == (360.0, 100.0, -70.0)
    assert (row["hole_type"], row["status"]) == ("RC", "abandoned")
    assert row["drill_date"] == datetime.date(2020, 1, 2)


async def test_first_upload_of_a_hole_still_defaults_hole_type_to_unknown(project: _Fixture) -> None:
    """NOT NULL columns: the INSERT path is unchanged."""
    await _write(project, [_collar("A-1", 2)])

    row = await _one(project)
    assert (row["hole_type"], row["status"]) == ("unknown", "unknown")


async def test_georef_method_and_uncertainty_follow_the_new_coordinates(project: _Fixture) -> None:
    await _write(project, [_collar("A-1", 2)], method="assumed")
    first = await _one(project)
    assert (first["georef_method"], first["spatial_uncertainty_m"]) == ("assumed", 175.0)

    # The same hole again, this time with a DECLARED CRS.
    await _write(project, [_collar("A-1", 2)], method="declared")

    row = await _one(project)
    assert row["georef_method"] == "declared"
    assert row["spatial_uncertainty_m"] == 35.0                  # re-derived by the trigger
    assert row["spatial_uncertainty_method"] == "modern_ni43101_declared"


async def test_crs_confidence_reaches_a_collar_matched_by_a_variant_spelling(project: _Fixture) -> None:
    await _write(project, [_collar("SRE09-6", 2)])
    # The second file spells it differently; the writer lands it on the stored collar.
    await _write(project, [_collar("SRE09_6", 2)])

    await _stamp_crs_confidence(
        project.conn, project_id=project.project_id, hole_ids=["SRE09_6"],
        confidence=0.7, flagged=set(),
    )
    row = await _one(project)
    assert row["hole_id"] == "SRE09-6"                           # stored spelling kept
    assert row["crs_confidence"] == pytest.approx(0.7)

    await _stamp_crs_confidence(
        project.conn, project_id=project.project_id, hole_ids=["SRE09_6"],
        confidence=0.7, flagged={"SRE09_6"},
    )
    assert (await _one(project))["crs_confidence"] == pytest.approx(0.1)


async def test_assays_without_a_sample_id_land_and_qaqc_is_not_pass(project: _Fixture) -> None:
    await _write(project, [_collar("DH-1", 2)])
    index = await _collar_index(project.conn, project.project_id)
    issues = RowIssues()

    def sample(row: int, a: float, b: float, qaqc: str) -> dict[str, Any]:
        return {
            "_source_row": row, "hole_id": "DH-1", "from_depth": a, "to_depth": b,
            "sample_type": "Core", "qaqc_type": qaqc, "commodity_assays": {"Au_ppm": 1.5},
        }

    stats = await _write_intervals(
        project.conn, workspace_id=project.workspace_id, sheet_type="sample",
        records=[sample(2, 0, 10, "Primary"), sample(3, 10, 20, "Blank")],
        index=index, issues=issues, source_file="comp.csv", source_file_sha256="a" * 64,
    )

    assert stats["assay_rows"] == 2 and stats["assay_rows_skipped"] == 0
    rows = await project.conn.fetch(
        "SELECT sample_id, from_depth, element, qaqc_flag FROM silver.assays_v2 "
        "WHERE collar_id = $1::uuid ORDER BY from_depth", index["DH-1"],
    )
    assert [(r["sample_id"], r["qaqc_flag"]) for r in rows] == [
        ("DH-1 0-10 m (no sample id)", None),
        ("DH-1 10-20 m (no sample id)", "control_blank"),
    ]
    assert len(issues.derived_sample_ids) == 2


async def test_geochemistry_records_its_source_and_reports_a_takeover(project: _Fixture) -> None:
    """Audit finding 21 against the real table and its partial unique index."""
    from app.hatchet_workflows.ingest_tabular import _write_surface_geochem

    shape = {
        "located": {"sample_id": "Sample", "easting": "X", "northing": "Y"},
        "assays": {"au_ppm": "Au_ppm", "cu_ppm": "Cu_ppm", "as_ppm": "As_ppm"},
    }

    def rows(*samples: str) -> list[dict[str, Any]]:
        return [
            {"Sample": s, "X": -105.5 + i * 0.001, "Y": 52.1, "Au_ppm": 0.1, "Cu_ppm": 20.0,
             "As_ppm": 5.0}
            for i, s in enumerate(samples)
        ]

    async def write(source: str, sha: str, *samples: str) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        await _write_surface_geochem(
            project.conn, workspace_id=project.workspace_id, project_id=project.project_id,
            shape=shape, rows=rows(*samples), source_epsg=4326,
            source_file=source, source_file_sha256=sha, warnings_out=warnings,
        )
        return warnings

    try:
        assert await write("north.dbf", "a" * 64, "S1", "S2") == []
        assert await write("north.dbf", "a" * 64, "S1", "S2") == []     # the same file again

        takeover = await write("south.dbf", "b" * 64, "S2", "S3")        # S2 is north.dbf's

        (note,) = takeover
        assert note["code"] == "geochemistry_sample_replaced_from_other_file"
        assert note["samples"] == ["S2"] and "'north.dbf'" in note["detail"]
        stored = {
            r["sample_id"]: (r["source_file"], r["source_file_sha256"], r["row_index"])
            for r in await project.conn.fetch(
                "SELECT sample_id, source_file, source_file_sha256, row_index "
                "FROM silver.geochemistry WHERE project_id = $1::uuid", project.project_id,
            )
        }
        assert stored == {
            "S1": ("north.dbf", "a" * 64, 0),
            "S2": ("south.dbf", "b" * 64, 0),       # last writer owns it, and it says who
            "S3": ("south.dbf", "b" * 64, 1),
        }
    finally:
        await project.conn.execute(
            "DELETE FROM silver.geochemistry WHERE project_id = $1::uuid", project.project_id,
        )
