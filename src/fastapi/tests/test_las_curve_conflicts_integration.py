"""The LAS curve conflict policy against a real Postgres (audit finding 5).

The decision rules and the warning text are test_las_curve_conflicts.py; this
runs the stored-curve read and ``ingest_las_file`` end to end against
``silver.well_log_curves`` and its real unique key.
"""
from __future__ import annotations

# ruff: noqa: F811 - the `project` fixture is imported, then named as a parameter
from pathlib import Path

import pytest

from app.hatchet_workflows.ingest_tabular import _write_collars
from app.services.ingest.las_curve_conflicts import fetch_stored_curves
from app.services.ingest.las_ingester import ingest_las_file

# Importing the sibling module also applies its module-level skip when no
# Postgres is configured, and brings the `project` fixture along.
from tests.test_ingest_constraint_rows_integration import (  # noqa: F401 - `project` is a fixture
    _collar,
    _Fixture,
    project,
)

pytestmark = pytest.mark.integration


def _las(path: Path, *, stop_ft: float = 1.0) -> Path:
    """A feet LAS for hole SB-001 carrying one GAMMA curve, 0 - ``stop_ft`` ft."""
    path.write_text(
        "~VERSION INFORMATION\n"
        " VERS.                 2.0 : CWLS LOG ASCII STANDARD - VERSION 2.0\n"
        " WRAP.                  NO : ONE LINE PER DEPTH STEP\n"
        "~WELL INFORMATION\n"
        "STRT .F          0.0 : START DEPTH\n"
        f"STOP .F   {stop_ft} : STOP DEPTH\n"
        "STEP .F          0.5 : STEP\n"
        "NULL .       -999.25 : NULL VALUE\n"
        "COMP .  ACME : COMPANY\n"
        "WELL .  SB-001 : WELL\n"
        "~CURVE INFORMATION\n"
        "DEPT .F  : DEPTH\n"
        "GAMMA.API : GAMMA RAY\n"
        "~ASCII\n"
        f"0.0   12.0\n0.5   14.0\n{stop_ft}   15.0\n",
        encoding="ascii",
    )
    return path


async def _ingest(project: _Fixture, path: Path):
    return await ingest_las_file(
        project.conn, str(path), workspace_id=project.workspace_id,
        project_id_override=project.project_id,
    )


async def _curve(project: _Fixture) -> dict:
    rows = await project.conn.fetch(
        "SELECT w.curve_name, w.source_file, w.min_depth, w.max_depth, w.depth_unit "
        "FROM silver.well_log_curves w JOIN silver.collars c USING (collar_id) "
        "WHERE c.project_id = $1::uuid", project.project_id,
    )
    assert len(rows) == 1                       # one hole, one GAMMA
    return dict(rows[0])


@pytest.fixture
async def hole(project: _Fixture):
    await _write_collars(
        project.conn, workspace_id=project.workspace_id, project_id=project.project_id,
        records=[_collar("SB-001", 2)], epsg=32613, georef_method="declared",
    )
    try:
        yield project
    finally:
        await project.conn.execute(
            "DELETE FROM silver.well_log_curves WHERE collar_id IN "
            "(SELECT collar_id FROM silver.collars WHERE project_id = $1::uuid)",
            project.project_id,
        )


async def test_the_second_run_replaces_the_first_and_says_so(hole: _Fixture, tmp_path: Path) -> None:
    first = await _ingest(hole, _las(tmp_path / "run1.las", stop_ft=100.0))
    assert first.warnings == [] and first.curves_inserted == 1

    second = await _ingest(hole, _las(tmp_path / "run2.las", stop_ft=200.0))   # 0-61 m covers 0-30 m

    (note,) = second.warnings
    assert note["code"] == "curve_replaced_from_other_file"
    assert "'run1.las'" in note["detail"] and "'run2.las'" in note["detail"]
    assert (await _curve(hole))["source_file"] == "run2.las"


async def test_reloading_the_same_file_is_silent(hole: _Fixture, tmp_path: Path) -> None:
    await _ingest(hole, _las(tmp_path / "run1.las", stop_ft=100.0))

    again = await _ingest(hole, _las(tmp_path / "run1.las", stop_ft=100.0))

    assert again.warnings == [] and again.curves_inserted == 1


async def test_a_complementary_run_is_refused_and_the_stored_curve_kept(hole: _Fixture, tmp_path: Path) -> None:
    await _ingest(hole, _las(tmp_path / "run1.las", stop_ft=100.0))
    # Make the stored curve a deeper run: 500 - 600 m, so the new file (0 - 30 m) is disjoint.
    await hole.conn.execute(
        "UPDATE silver.well_log_curves SET min_depth = 500, max_depth = 600 "
        "WHERE collar_id IN (SELECT collar_id FROM silver.collars WHERE project_id = $1::uuid)",
        hole.project_id,
    )

    second = await _ingest(hole, _las(tmp_path / "run2.las", stop_ft=100.0))

    assert second.curves_inserted == 0
    assert [w["code"] for w in second.warnings] == ["curve_replacement_refused"]
    kept = await _curve(hole)
    assert kept["source_file"] == "run1.las" and kept["min_depth"] == 500


async def test_the_stored_curve_read_converts_feet(hole: _Fixture, tmp_path: Path) -> None:
    await _ingest(hole, _las(tmp_path / "run1.las", stop_ft=100.0))
    await hole.conn.execute(
        "UPDATE silver.well_log_curves SET depth_unit = 'ft', min_depth = 100, max_depth = 200 "
        "WHERE collar_id IN (SELECT collar_id FROM silver.collars WHERE project_id = $1::uuid)",
        hole.project_id,
    )
    collar_id = await hole.conn.fetchval(
        "SELECT collar_id FROM silver.collars WHERE project_id = $1::uuid", hole.project_id,
    )

    stored = await fetch_stored_curves(hole.conn, str(collar_id), ["GAMMA", "OTHER"])

    assert set(stored) == {"GAMMA"}
    assert (stored["GAMMA"].min_depth, stored["GAMMA"].max_depth) == pytest.approx((30.48, 60.96))
