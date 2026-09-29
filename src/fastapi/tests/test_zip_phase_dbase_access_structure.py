"""ZIP phase split for structure tables, standalone dBASE/DAT tables and
Access databases.

ingest_tabular now routes the tables inside .dbf/.dat/.mdb/.accdb through
the same classifier as a CSV, and writes structure measurements to
silver.structure against a collar. Those members therefore depend on the
archive's collar tables exactly like a lithology CSV does, and the two-phase
dispatch in ingest_zip_archive must defer them -- or the same "interval ran
before its collars and orphaned every row" failure returns for the formats
Red Star actually ships.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.hatchet_workflows import ingest_tabular as tabular
from app.hatchet_workflows import ingest_zip_archive as module

COLLAR_ROWS = [{"HOLE_ID": "H1", "EASTING": "471000", "NORTHING": "4657000",
                "ELEVATION": "2000", "TOTAL_DEPTH": "120"}]
LITHO_ROWS = [{"HOLE_ID": "H1", "FROM": "0", "TO": "5", "LITH_CODE": "SST"}]
STRUCTURE_CSV = "hole_id,depth,structure_type,alpha,beta,dip,dip_dir\nH1,10,fault,40,120,55,210\n"


def _touch(dir_: Path, name: str, text: str = "x") -> Path:
    path = dir_ / name
    path.write_text(text, encoding="utf-8")
    return path


async def test_a_structure_table_waits_for_its_collars(tmp_path: Path) -> None:
    structure = _touch(tmp_path, "structure.csv", STRUCTURE_CSV)

    producers, dependents = await module._split_into_phases([structure])

    assert (producers, dependents) == ([], [structure])


async def test_a_standalone_dbf_of_intervals_is_phase_two_and_of_collars_phase_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    litho = _touch(tmp_path, "Lith.dbf")
    collars = _touch(tmp_path, "Collars.dbf")
    rows = {"Lith.dbf": LITHO_ROWS, "Collars.dbf": COLLAR_ROWS}
    monkeypatch.setattr(tabular, "_read_dbf_table", lambda p: rows[Path(p).name])

    producers, dependents = await module._split_into_phases([litho, collars])

    assert producers == [collars]
    assert dependents == [litho]


async def test_a_dbf_beside_its_shapefile_is_a_sidecar_and_never_sniffed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbf = _touch(tmp_path, "Holes.dbf")
    _touch(tmp_path, "Holes.shp")

    def boom(_path: str) -> list:
        raise AssertionError("a shapefile sidecar must not be read as a table")

    monkeypatch.setattr(tabular, "_read_dbf_table", boom)

    producers, dependents = await module._split_into_phases([dbf])

    assert (producers, dependents) == ([dbf], [])


async def test_a_mapinfo_dat_uses_the_dat_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dat = _touch(tmp_path, "Sitka_lith.DAT")
    monkeypatch.setattr(tabular, "_read_mapinfo_dat_table", lambda _p: LITHO_ROWS)

    producers, dependents = await module._split_into_phases([dat])

    assert dependents == [dat]


async def test_an_access_database_is_deferred_only_when_it_holds_no_collar_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    only_intervals = _touch(tmp_path, "logs.mdb")
    with_collars = _touch(tmp_path, "project.accdb")
    contents = {
        "logs.mdb": {"Lith": LITHO_ROWS},
        "project.accdb": {"Collars": COLLAR_ROWS, "Lith": LITHO_ROWS},
    }
    import georag_geoparsers.access_mdb as access

    monkeypatch.setattr(access, "list_tables", lambda p: list(contents[Path(p).name]))
    monkeypatch.setattr(
        access, "read_table", lambda p, name: contents[Path(p).name][name],
    )

    producers, dependents = await module._split_into_phases([only_intervals, with_collars])

    assert producers == [with_collars]
    assert dependents == [only_intervals]


async def test_an_unreadable_access_database_stays_in_phase_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mdb = _touch(tmp_path, "broken.mdb")
    import georag_geoparsers.access_mdb as access

    def boom(_p: str) -> list:
        raise RuntimeError("mdbtools missing")

    monkeypatch.setattr(access, "list_tables", boom)

    producers, dependents = await module._split_into_phases([mdb])

    assert (producers, dependents) == ([mdb], [])
