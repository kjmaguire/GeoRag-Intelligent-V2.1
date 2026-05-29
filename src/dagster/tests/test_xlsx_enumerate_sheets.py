"""Tests for enumerate_sheets + multi-sheet classification (2026-05-23).

Builds synthetic multi-sheet XLSX files with openpyxl and asserts:
  * Every sheet is enumerated with its headers + row count
  * Hidden sheets surface with hidden=True
  * Empty sheets report row_count=0
  * Headers route to the right sheet_type via the classifier
  * The combination of enumerate_sheets + classify_sheet_type is what
    silver_xlsx auto-dispatch mode uses to decide which sheets to
    parse + insert
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def _build_multisheet_xlsx(sheets: dict, *, hidden_sheets: set[str] | None = None) -> str:
    """Build a workbook from ``{sheet_name: [header_row, *data_rows]}``."""
    import openpyxl

    hidden_sheets = hidden_sheets or set()
    wb = openpyxl.Workbook()
    # Drop the default sheet so the test fixture is purely what we build.
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
        if sheet_name in hidden_sheets:
            ws.sheet_state = "hidden"

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    import os
    os.close(fd)
    wb.save(path)
    wb.close()
    return path


def test_enumerate_sheets_returns_one_per_sheet():
    from georag_dagster.parsers.xlsx_parser import enumerate_sheets

    path = _build_multisheet_xlsx({
        "Collars":   [["HoleID", "Easting", "Northing", "Elevation"], ["DH01", 500000, 5000000, 350]],
        "Surveys":   [["HoleID", "Depth", "Azimuth", "Dip"], ["DH01", 10, 45, -60]],
        "Lithology": [["HoleID", "From", "To", "LithCode"], ["DH01", 0, 5, "GRA"]],
        "Notes":     [["Author", "Date", "Comment"]],
    })
    try:
        sheets = enumerate_sheets(path)
        names = [s.name for s in sheets]
        assert names == ["Collars", "Surveys", "Lithology", "Notes"]
        # Verify row counts
        by_name = {s.name: s for s in sheets}
        assert by_name["Collars"].row_count == 1
        assert by_name["Surveys"].row_count == 1
        assert by_name["Lithology"].row_count == 1
        assert by_name["Notes"].row_count == 0
    finally:
        Path(path).unlink(missing_ok=True)


def test_enumerate_sheets_classifies_each_correctly():
    from georag_dagster.parsers.xlsx_parser import enumerate_sheets

    path = _build_multisheet_xlsx({
        "Collars":   [["HoleID", "Easting", "Northing", "Elevation"]],
        "Surveys":   [["HoleID", "Depth", "Azimuth", "Dip"]],
        "Lithology": [["HoleID", "From", "To", "LithCode"]],
        "Samples":   [["HoleID", "From", "To", "SampleType"]],
        "Notes":     [["Author", "Date", "Comment"]],
    })
    try:
        by_name = {s.name: s for s in enumerate_sheets(path)}
        assert by_name["Collars"].sheet_type == "collar"
        assert by_name["Surveys"].sheet_type == "survey"
        assert by_name["Lithology"].sheet_type == "lithology"
        assert by_name["Samples"].sheet_type == "sample"
        assert by_name["Notes"].sheet_type == "unknown"
        # Notes confidence is 0 — classifier returns 0.0 for unknown.
        assert by_name["Notes"].classify_confidence == 0.0
        # The four known sheets should hit 1.0 confidence on these
        # canonical headers.
        for known in ("Collars", "Surveys", "Lithology", "Samples"):
            assert by_name[known].classify_confidence == 1.0, (
                f"{known} confidence was {by_name[known].classify_confidence}"
            )
    finally:
        Path(path).unlink(missing_ok=True)


def test_enumerate_sheets_surfaces_hidden_state():
    from georag_dagster.parsers.xlsx_parser import enumerate_sheets

    path = _build_multisheet_xlsx(
        {
            "Visible":   [["HoleID", "Easting", "Northing", "Elevation"]],
            "Scratch":   [["HoleID", "Depth", "Azimuth", "Dip"]],
        },
        hidden_sheets={"Scratch"},
    )
    try:
        by_name = {s.name: s for s in enumerate_sheets(path)}
        assert by_name["Visible"].hidden is False
        assert by_name["Scratch"].hidden is True
    finally:
        Path(path).unlink(missing_ok=True)


def test_enumerate_sheets_handles_empty_sheet():
    from georag_dagster.parsers.xlsx_parser import enumerate_sheets

    path = _build_multisheet_xlsx({
        "Empty": [],
    })
    try:
        sheets = enumerate_sheets(path)
        assert len(sheets) == 1
        # Empty sheet → empty headers, zero rows, classifies as unknown.
        assert sheets[0].name == "Empty"
        assert sheets[0].row_count == 0
        assert sheets[0].sheet_type == "unknown"
    finally:
        Path(path).unlink(missing_ok=True)


def test_enumerate_sheets_rejects_unsupported_extension(tmp_path):
    from georag_dagster.parsers.xlsx_parser import enumerate_sheets

    bogus = tmp_path / "notxlsx.txt"
    bogus.write_text("hello")
    with pytest.raises(ValueError, match="unsupported extension"):
        enumerate_sheets(str(bogus))
