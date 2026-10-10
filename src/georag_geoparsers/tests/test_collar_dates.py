"""Drill dates: Excel date-times are read, ambiguous day/month is never guessed.

WHY THIS FILE EXISTS (audit finding 15)
    * A workbook's date cell reaches the CSV parser as
      ``2023-04-05T00:00:00.000000`` (Polars writes the sheet to an in-memory
      CSV). The parser's format list could not read it, so EVERY ``drill_date``
      of an .xlsx collar sheet was NULL, with nothing in the run to say so.
    * ``03/04/2023`` matched ``%d/%m/%Y`` first and was stored as 3 April
      whichever the file meant.
    * Anything unreadable was dropped without a trace.
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest

from georag_geoparsers._dates import DateReader
from georag_geoparsers.csv_collar import parse_csv_collars
from georag_geoparsers.xlsx_parser import parse_xlsx_sheet

HEADER = "HoleID,Easting,Northing,Elevation,Depth,DrillDate\n"


def _collars(*dates: str) -> str:
    rows = [
        f"DH-{i},{500000 + i},{6000000 + i},300,150,{value}"
        for i, value in enumerate(dates, start=1)
    ]
    return HEADER + "\n".join(rows) + "\n"


def _parse(*dates: str):
    result = parse_csv_collars(io.StringIO(_collars(*dates)))
    return [r["drill_date"] for r in result.records], result


def _codes(result) -> list[str]:
    return [w["code"] for w in result.warnings]


class TestExcelDateTimesAreRead:
    @pytest.mark.parametrize("text", [
        "2023-04-05T00:00:00.000000",       # what Polars writes for an Excel date
        "2023-04-05T00:00:00",
        "2023-04-05 00:00:00",
        "2023-04-05 10:30:15.123456",
        "2023-04-05T10:30:15Z",
        "2023-04-05T10:30:15+02:00",
        "2023-04-05",
        "20230405",
        "05-Apr-2023",
    ])
    def test_the_date_it_names(self, text: str) -> None:
        dates, result = _parse(text)
        assert dates == [date(2023, 4, 5)]
        assert not {"date_unparseable", "date_ambiguous"} & set(_codes(result))

    def test_a_real_workbook_date_cell_survives_end_to_end(self, tmp_path) -> None:
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Collars"
        ws.append(["HoleID", "Easting", "Northing", "Elevation", "Depth", "DrillDate"])
        ws.append(["DH-1", 500000, 6000000, 300, 150, datetime(2023, 4, 5)])
        ws.append(["DH-2", 500100, 6000100, 310, 160, datetime(2022, 11, 30, 14, 15)])
        path = tmp_path / "collars.xlsx"
        wb.save(path)

        result = parse_xlsx_sheet(str(path), "Collars", "collar")

        assert [r["drill_date"] for r in result.records] == [
            date(2023, 4, 5), date(2022, 11, 30),
        ]
        assert not {"date_unparseable", "date_ambiguous"} & {
            w["code"] for w in result.warnings
        }


class TestDayMonthOrderIsNeverGuessed:
    def test_an_ambiguous_date_alone_is_left_empty_and_reported_with_both_readings(self) -> None:
        dates, result = _parse("03/04/2023")

        assert dates == [None]
        (warning,) = [w for w in result.warnings if w["code"] == "date_ambiguous"]
        assert warning["context"]["rows"] == [{
            "row": 2, "value": "03/04/2023",
            "day_first": "2023-04-03", "month_first": "2023-03-04",
        }]
        assert "03 Apr 2023" in warning["detail"] and "04 Mar 2023" in warning["detail"]
        assert "YYYY-MM-DD" in warning["detail"]

    def test_a_day_over_twelve_elsewhere_fixes_the_file_as_day_first(self) -> None:
        dates, result = _parse("25/04/2023", "03/04/2023")

        assert dates == [date(2023, 4, 25), date(2023, 4, 3)]
        (note,) = [w for w in result.warnings if w["code"] == "date_convention_inferred"]
        assert note["severity"] == "info"
        assert note["context"] == {"convention": "dmy", "evidence_row": 2, "count": 1}
        assert "25/04/2023" in note["detail"]
        assert "date_ambiguous" not in _codes(result)

    def test_a_month_over_twelve_elsewhere_fixes_the_file_as_month_first(self) -> None:
        dates, result = _parse("04/25/2023", "03/04/2023")

        assert dates == [date(2023, 4, 25), date(2023, 3, 4)]
        assert "date_convention_inferred" in _codes(result)

    def test_both_orders_in_one_column_leave_the_ambiguous_ones_empty(self) -> None:
        dates, result = _parse("25/04/2023", "04/25/2023", "03/04/2023")

        assert dates == [date(2023, 4, 25), date(2023, 4, 25), None]
        warning = next(w for w in result.warnings if w["code"] == "date_ambiguous")
        assert "both a day-first and a month-first" in warning["detail"]

    def test_day_equals_month_is_not_ambiguous(self) -> None:
        dates, result = _parse("05/05/2023")
        assert dates == [date(2023, 5, 5)]
        assert not {"date_ambiguous", "date_convention_inferred"} & set(_codes(result))

    @pytest.mark.parametrize("text", ["5.4.2023", "05-04-2023"])
    def test_other_separators_follow_the_same_rule(self, text: str) -> None:
        dates, result = _parse(text)
        assert dates == [None]
        assert "date_ambiguous" in _codes(result)

    def test_an_iso_date_is_never_ambiguous_beside_slash_dates(self) -> None:
        dates, _result = _parse("2023-04-03", "03/04/2023")
        assert dates[0] == date(2023, 4, 3)
        assert dates[1] is None


class TestUnreadableDatesAreReported:
    def test_a_non_empty_value_that_is_not_a_date_is_named(self) -> None:
        dates, result = _parse("2023-04-05", "sometime in spring", "31/02/2023")

        assert dates == [date(2023, 4, 5), None, None]
        (warning,) = [w for w in result.warnings if w["code"] == "date_unparseable"]
        assert warning["context"]["count"] == 2
        assert [r["value"] for r in warning["context"]["rows"]] == [
            "sometime in spring", "31/02/2023",
        ]
        assert "row 3 'sometime in spring'" in warning["detail"]

    def test_the_row_is_still_valid(self) -> None:
        _dates, result = _parse("sometime in spring")
        assert result.valid_rows == 1 and result.skipped_rows == 0

    def test_empty_cells_are_not_a_problem(self) -> None:
        dates, result = _parse("", "2023-04-05")
        assert dates == [None, date(2023, 4, 5)]
        assert not {"date_unparseable", "date_ambiguous"} & set(_codes(result))


class TestTheReaderDirectly:
    def test_date_objects_pass_through(self) -> None:
        reader = DateReader([])
        assert reader.read(2, datetime(2023, 4, 5, 9, 30)) == date(2023, 4, 5)
        assert reader.read(3, date(2023, 4, 6)) == date(2023, 4, 6)
        assert reader.warnings() == []

    def test_no_date_column_costs_nothing(self) -> None:
        reader = DateReader([])
        assert reader.convention is None and reader.warnings() == []
