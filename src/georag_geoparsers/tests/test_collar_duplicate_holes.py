"""split_duplicate_holes / csv_collar keep the first row of a repeated hole.

WHY THIS FILE EXISTS (audit finding 6)
    A hole listed twice in one collar file reached the database as two
    upserts onto one collar and the LAST row won, silently. The first row is
    kept deliberately now and every repeat is reported with both positions.
    The writer side (``ingest_tabular._write_collars``) is tested in
    src/fastapi/tests/test_collar_duplicate_hole_ids.py.
"""

from __future__ import annotations

import io

from georag_geoparsers._hole_id import (
    duplicate_hole_skip_entry,
    duplicate_hole_warning,
    split_duplicate_holes,
)
from georag_geoparsers.csv_collar import parse_csv_collars


def _rec(hole: str, row: int, e: float, n: float, **extra) -> dict:
    return {"hole_id": hole, "_source_row": row, "easting": e, "northing": n, **extra}


class TestSplitDuplicateHoles:
    def test_first_wins_across_spellings_and_order_is_kept(self) -> None:
        records = [
            _rec("DH-1", 2, 1.0, 1.0), _rec("DH-2", 3, 2.0, 2.0),
            _rec("dh_1", 4, 9.0, 9.0), _rec("DH2", 5, 2.0, 2.0),
        ]
        kept, duplicates = split_duplicate_holes(records)

        assert [r["hole_id"] for r in kept] == ["DH-1", "DH-2"]
        assert [(d["row"], d["first_row"], d["identical"]) for d in duplicates] == [
            (4, 2, False), (5, 3, True),
        ]

    def test_records_without_an_id_are_never_duplicates_of_each_other(self) -> None:
        kept, duplicates = split_duplicate_holes(
            [_rec("", 2, 1.0, 1.0), _rec("", 3, 2.0, 2.0)],
        )
        assert len(kept) == 2 and duplicates == []

    def test_nothing_repeated_is_returned_unchanged(self) -> None:
        records = [_rec("A", 2, 1.0, 1.0), _rec("B", 3, 2.0, 2.0)]
        kept, duplicates = split_duplicate_holes(records)
        assert kept == records and duplicates == []


class TestTheReport:
    def test_the_warning_has_rows_coordinates_and_a_severity_rule(self) -> None:
        _kept, duplicates = split_duplicate_holes(
            [_rec("DH-1", 2, 500000.0, 6000000.0), _rec("DH-1", 7, 500100.0, 6000100.0)],
        )
        warning = duplicate_hole_warning(duplicates, label="collars.csv")

        assert warning["code"] == "duplicate_hole_id"
        assert "collars.csv" in warning["message"]
        # A northing of 6,000,100 must not be printed as 6.0001e+06.
        assert "E 500100, N 6000100" in warning["detail"]
        assert "E 500000, N 6000000" in warning["detail"]
        assert "severity" not in warning

    def test_an_identical_repeat_is_information(self) -> None:
        _kept, duplicates = split_duplicate_holes(
            [_rec("DH-1", 2, 1.0, 1.0), _rec("DH-1", 3, 1.0, 1.0)],
        )
        assert duplicate_hole_warning(duplicates)["severity"] == "info"

    def test_no_duplicates_no_warning(self) -> None:
        assert duplicate_hole_warning([]) is None

    def test_the_skip_entry_follows_the_parser_shape(self) -> None:
        _kept, (dup,) = split_duplicate_holes(
            [_rec("DH-1", 2, 1.0, 1.0), _rec("DH-1", 3, 5.0, 5.0)],
        )
        entry = duplicate_hole_skip_entry(dup)
        assert {"row", "code", "reason", "raw", "expected", "actual", "suggestion"} <= set(entry)
        assert entry["code"] == "duplicate_hole_id" and entry["row"] == 3


class TestCsvCollar:
    def test_a_repeated_hole_is_skipped_and_the_first_is_stored(self) -> None:
        text = (
            "HoleID,Easting,Northing,Elevation,Depth\n"
            "DH-1,500000,6000000,300,150\n"
            "DH-1,500999,6000999,305,160\n"
        )
        result = parse_csv_collars(io.StringIO(text))

        assert [r["easting"] for r in result.records] == [500000.0]
        assert [s["code"] for s in result.skipped_details] == ["duplicate_hole_id"]
        assert any(w["code"] == "duplicate_hole_id" for w in result.warnings)
        assert result.total_rows == result.valid_rows + result.skipped_rows
