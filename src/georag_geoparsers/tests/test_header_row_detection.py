"""A title or comment block above the header no longer hides a table (ING-13).

Audit 2026-09-29: an .xlsx sheet with "Acme Gold Corp - Drill Collar Table"
in A1 and the header in row 3 classified ``unknown`` (text fallback only); a
CSV with two ``# ...`` comment lines parsed 0 records ("missing required
columns"). The same data with the header on the first line was fine.

Also pinned: .xlsx is read through openpyxl. polars' default engine needs
``fastexcel``, which no lockfile installs, so the default read raised.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from georag_geoparsers import parse_csv_collars, parse_csv_samples
from georag_geoparsers._csv_io import count_preamble_lines, open_csv_with_encoding
from georag_geoparsers._sheet_classifier import detect_header_row

openpyxl = pytest.importorskip("openpyxl")

_HEADER = ["Hole_ID", "Easting", "Northing", "Elevation", "Total_Depth", "Azimuth", "Dip"]


def _workbook(path: Path, *, title: bool) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Collars"
    if title:
        ws.append(["Acme Gold Corp - Drill Collar Table"])
        ws.append([])
    ws.append(_HEADER)
    ws.append(["DH-001", 500000, 6000000, 400, 150, 90, -60])
    ws.append(["DH-002", 500100, 6000100, 410, 200, 45, -55])
    wb.save(path)
    return path


class TestDetectHeaderRow:
    def test_row_zero_is_kept_when_it_classifies(self) -> None:
        assert detect_header_row([_HEADER, ["DH-1", 1, 2, 3, 4, 5, -6]]) == 0

    def test_title_rows_are_skipped(self) -> None:
        rows = [["Acme Gold Corp - Drill Collar Table"], [], _HEADER, ["DH-1", 1, 2]]
        assert detect_header_row(rows) == 2

    def test_nothing_recognisable_stays_on_row_zero(self) -> None:
        assert detect_header_row([["Notes"], ["a", "b"], ["c", "d"]]) == 0


class TestWorkbook:
    def test_enumerate_finds_the_header_below_a_title(self, tmp_path: Path) -> None:
        from georag_geoparsers.xlsx_parser import enumerate_sheets

        meta = enumerate_sheets(str(_workbook(tmp_path / "t.xlsx", title=True)))[0]
        assert (meta.sheet_type, meta.header_row, meta.row_count) == ("collar", 2, 2)
        assert meta.headers[:3] == ["Hole_ID", "Easting", "Northing"]

    def test_the_sheet_parses_and_says_what_it_skipped(self, tmp_path: Path) -> None:
        from georag_geoparsers.xlsx_parser import parse_xlsx_sheet

        result = parse_xlsx_sheet(
            str(_workbook(tmp_path / "t.xlsx", title=True)), "Collars", "collar",
        )
        assert [r["hole_id"] for r in result.records] == ["DH-001", "DH-002"]
        assert result.records[0]["total_depth"] == 150.0
        note = next(w for w in result.warnings if w["code"] == "header_row_detected")
        assert "row 3" in note["message"] and note["detail"]

    def test_a_plain_sheet_reads_without_fastexcel(self, tmp_path: Path) -> None:
        from georag_geoparsers.xlsx_parser import parse_xlsx_sheet, read_sheet_rows

        path = str(_workbook(tmp_path / "p.xlsx", title=False))
        result = parse_xlsx_sheet(path, "Collars", "collar")
        assert result.valid_rows == 2
        assert not [w for w in result.warnings if w["code"] == "header_row_detected"]
        assert len(read_sheet_rows(path, "Collars")) == 2


class TestCsvPreamble:
    @pytest.mark.parametrize(("text", "expected"), [
        ("a,b,c\n1,2,3\n", 0),
        ("#HoleID,From,To\nA,1,2\n", 0),               # a '#' header is still a header
        ("Title\n\n# exported, 2024\nHole,E,N,Z\nA,1,2,3\nB,1,2,3\n", 3),
        ("Hole,From,To,Au\nA,0,1,\nA,1,2,\n", 0),       # blank trailing cells
        ("x\ny\n", 0),                                   # one column: not judged
        ("Sample;Au_ppm\nP1;0,016\n", 0),               # ';' table, decimal comma
    ])
    def test_count(self, text: str, expected: int) -> None:
        assert count_preamble_lines(text) == expected

    def test_comment_lines_no_longer_hide_the_collars(self) -> None:
        text = (
            "# Exported from logging db 2019-03-01\n# Units: metres\n"
            "Hole_ID,Easting,Northing,Total_Depth\nDH-001,500000,6000000,150\n"
        )
        result = parse_csv_collars(io.StringIO(text))
        assert [r["hole_id"] for r in result.records] == ["DH-001"]

    def test_a_titled_assay_export_parses(self) -> None:
        text = (
            "ALS Certificate VA19000001\n\n"
            "Hole,SampleID,From,To,Au (g/t)\nDH-1,S1,0,1,0.5\n"
        )
        result = parse_csv_samples(io.StringIO(text))
        assert result.records[0]["commodity_assays"] == {"Au_ppm": 0.5}

    def test_the_stream_records_what_was_skipped_and_the_hash_is_of_the_upload(self) -> None:
        text = "# a\n# b\nHole,E,N\nA,1,2\n"
        stream, _enc, sha, size = open_csv_with_encoding(io.StringIO(text))
        assert stream.preamble_lines == 2
        assert stream.getvalue().startswith("Hole,E,N")
        assert size == len(text.encode("utf-8"))
