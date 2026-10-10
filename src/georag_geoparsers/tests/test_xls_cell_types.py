"""A legacy .xls cell is read by its TYPE, not ``str()``-ed (audit finding 2).

The .xls path used to hand ``str(cell.value)`` to the CSV parsers, which made
three values silently wrong:

  * an Excel ERROR cell (``#N/A``, ``#DIV/0!``) is stored as a small integer
    code, so it became the text "42" / "7" and was read as a real elevation or
    depth;
  * a numeric hole id is a float in BIFF, so 1001 became "1001.0", which
    ``_hole_id.canonicalize`` reduces to "10010" - the key of a DIFFERENT hole;
  * a date-formatted cell was its serial number ("45021.0"), which the date
    parser could not read, and a repeated header kept only its last column.

``fixtures/legacy_drill.xls`` is a committed real BIFF8 workbook carrying each
of those (regenerate with ``fixtures/make_legacy_xls_fixtures.py``; xlwt is not
a project dependency, which is why the binary is checked in).
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

xlrd = pytest.importorskip("xlrd")

from georag_geoparsers.xlsx_parser import (  # noqa: E402
    _CellNotes,
    _xls_cell_text,
    _xls_to_polars_df,
    parse_xlsx_sheet,
    read_sheet_rows,
    read_xls_sheets,
)

FIXTURES = Path(__file__).parent / "fixtures"
DRILL = str(FIXTURES / "legacy_drill.xls")


def _cell(ctype: int, value) -> object:
    return xlrd.sheet.Cell(ctype, value)


def _text(ctype: int, value, *, datemode: int = 0, notes: _CellNotes | None = None) -> str:
    return _xls_cell_text(_cell(ctype, value), datemode, notes or _CellNotes(), 0, 0)


class TestCellText:
    """One cell at a time, on hand-built xlrd cells (no workbook needed)."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1001.0, "1001"),          # an integral float is its integer, not "1001.0"
            (10010.0, "10010"),
            (0.0, "0"),
            (-5.0, "-5"),
            (6000000.25, "6000000.25"),
            (0.1, "0.1"),              # shortest round trip, no float noise
            (1.5e20, "1.5e+20"),       # too large to be an integer id: kept exact
            (float("nan"), ""),
        ],
    )
    def test_numbers(self, value: float, expected: str) -> None:
        assert _text(xlrd.XL_CELL_NUMBER, value) == expected

    def test_text_is_untouched(self) -> None:
        assert _text(xlrd.XL_CELL_TEXT, " DH-1 ") == " DH-1 "

    def test_empty_and_blank_cells_are_empty(self) -> None:
        assert _text(xlrd.XL_CELL_EMPTY, "") == ""
        assert _text(xlrd.XL_CELL_BLANK, "") == ""

    @pytest.mark.parametrize(("value", "expected"), [(1, "true"), (0, "false")])
    def test_booleans(self, value: int, expected: str) -> None:
        assert _text(xlrd.XL_CELL_BOOLEAN, value) == expected

    @pytest.mark.parametrize(
        ("code", "shown"),
        [(0x00, "#NULL!"), (0x07, "#DIV/0!"), (0x0F, "#VALUE!"), (0x17, "#REF!"),
         (0x1D, "#NAME?"), (0x24, "#NUM!"), (0x2A, "#N/A")],
    )
    def test_an_error_cell_is_empty_not_its_code_and_is_recorded(
        self, code: int, shown: str,
    ) -> None:
        notes = _CellNotes()
        cell = _cell(xlrd.XL_CELL_ERROR, code)

        # Row 2, column D (0-based 1, 3): the A1 name is what a person can find.
        assert _xls_cell_text(cell, 0, notes, 1, 3) == ""
        assert notes.errors == [("D2", shown)]

    def test_a_date_is_iso_text(self) -> None:
        serial = xlrd.xldate.xldate_from_date_tuple((2023, 4, 5), 0)
        assert _text(xlrd.XL_CELL_DATE, serial) == "2023-04-05"

    def test_a_date_with_a_time_keeps_the_time(self) -> None:
        serial = xlrd.xldate.xldate_from_datetime_tuple((2023, 4, 5, 13, 30, 0), 0)
        assert _text(xlrd.XL_CELL_DATE, serial) == "2023-04-05T13:30:00"

    def test_a_time_of_day_is_not_turned_into_a_1899_date(self) -> None:
        assert _text(xlrd.XL_CELL_DATE, 0.5) == "12:00:00"

    def test_the_1904_date_system_is_honoured(self) -> None:
        serial = xlrd.xldate.xldate_from_date_tuple((2023, 4, 5), 1)
        assert _text(xlrd.XL_CELL_DATE, serial, datemode=1) == "2023-04-05"

    def test_a_date_that_cannot_be_converted_is_kept_as_a_number_and_noted(self) -> None:
        notes = _CellNotes()
        cell = _cell(xlrd.XL_CELL_DATE, 1e12)       # far past year 9999

        text = _xls_cell_text(cell, 0, notes, 4, 5)

        assert text == "1000000000000"
        assert notes.bad_dates == [("F5", "1000000000000")]
        assert [w["code"] for w in notes.warnings("S")] == ["excel_date_unconverted"]


class TestLegacyDrillWorkbook:
    """The same conversions end to end, on a real BIFF8 file."""

    def test_the_frame_holds_text_by_type(self) -> None:
        df, name, warnings = _xls_to_polars_df(DRILL, "Collars", header_row=0)

        assert name == "Collars"
        # The second "Notes" column is kept under a distinct name, not lost.
        assert df.columns == [
            "HoleID", "Easting", "Northing", "Elevation", "Total_Depth",
            "DrillDate", "Notes", "Notes_7", "Verified",
        ]
        assert df.rows() == [
            ("1001", "500000.5", "6000000.25", "300", "150", "2023-04-05",
             "first note", "second note", "true"),
            ("10010", "500100", "6000100", "", "160", "2022-11-30", "", "", ""),
            ("DH-3", "500200", "6000200", "320", "", "", "", "", ""),
        ]
        assert [w["code"] for w in warnings] == [
            "excel_error_cells", "duplicate_header_renamed",
        ]

    def test_the_error_warning_names_each_cell(self) -> None:
        _df, _name, warnings = _xls_to_polars_df(DRILL, "Collars", header_row=0)

        errors = next(w for w in warnings if w["code"] == "excel_error_cells")
        assert errors["context"]["count"] == 2
        assert errors["context"]["cells"] == [
            {"cell": "D3", "error": "#N/A"},
            {"cell": "E4", "error": "#DIV/0!"},
        ]
        # message AND detail: the Ingestion Runs page renders `detail`.
        assert errors["message"] and "D3" in errors["detail"] and "E4" in errors["detail"]

    def test_the_duplicate_header_warning_is_informational(self) -> None:
        _df, _name, warnings = _xls_to_polars_df(DRILL, "Collars", header_row=0)

        renamed = next(w for w in warnings if w["code"] == "duplicate_header_renamed")
        assert renamed["severity"] == "info"
        assert renamed["context"]["renamed"] == [("Notes", "Notes_7")]

    def test_collar_parse_keeps_distinct_numeric_holes_distinct(self) -> None:
        result = parse_xlsx_sheet(DRILL, "Collars", "collar")

        by_id = {r["hole_id"]: r for r in result.records}
        assert sorted(by_id) == ["1001", "10010", "DH-3"]
        # "1001.0" used to canonicalise to "10010" - the other hole's key.
        assert by_id["1001"]["hole_id_canonical"] != by_id["10010"]["hole_id_canonical"]
        assert result.skipped_rows == 0

    def test_an_error_cell_is_not_read_as_its_numeric_code(self) -> None:
        result = parse_xlsx_sheet(DRILL, "Collars", "collar")

        by_id = {r["hole_id"]: r for r in result.records}
        assert by_id["10010"]["elevation"] is None          # #N/A, not 42
        assert by_id["DH-3"]["total_depth"] is None         # #DIV/0!, not 7
        assert by_id["1001"]["elevation"] == 300.0
        codes = [w["code"] for w in result.warnings]
        assert "excel_error_cells" in codes

    def test_date_cells_are_dates(self) -> None:
        result = parse_xlsx_sheet(DRILL, "Collars", "collar")

        by_id = {r["hole_id"]: r for r in result.records}
        assert by_id["1001"]["drill_date"] == datetime.date(2023, 4, 5)
        assert by_id["10010"]["drill_date"] == datetime.date(2022, 11, 30)
        assert by_id["DH-3"]["drill_date"] is None
        assert not [w for w in result.warnings if w["code"].startswith("date_")]

    def test_read_sheet_rows_shares_the_conversion(self) -> None:
        rows = read_sheet_rows(DRILL, "Collars")

        assert [r["HoleID"] for r in rows] == ["1001", "10010", "DH-3"]
        assert rows[1]["Elevation"] == ""
        assert rows[0]["Notes_7"] == "second note"

    def test_the_text_fallback_reader_shares_the_conversion(self) -> None:
        """``read_xls_sheets`` feeds app/services/ingest/xlsx_ingester.py."""
        sheets = dict(read_xls_sheets(DRILL))

        lines = sheets["Collars"].split("\n")
        header = lines[0].split("\t")
        assert header[0] == "HoleID" and header[-1] == "Verified"
        first = lines[1].split("\t")
        assert first[0] == "1001"
        assert first[5] == "2023-04-05"
        # The #N/A cell is empty - "42" must not appear anywhere in row 2.
        second = lines[2].split("\t")
        assert second[0] == "10010" and second[3] == ""
        assert "42" not in second
