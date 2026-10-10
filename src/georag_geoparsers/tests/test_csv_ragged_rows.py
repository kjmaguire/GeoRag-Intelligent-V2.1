"""A CSV line wider than its header is skipped and reported, never shifted.

WHY THIS FILE EXISTS (audit finding 9)
    Every drill CSV parser read its file with ``truncate_ragged_lines=True``.
    A comma inside an unquoted value makes the line one field wider than the
    header; Polars then dropped the SURPLUS field - the last one - and kept
    the rest in place, which moved every value after the stray comma one
    column to the left:

        hole,from,to,lith,description,color
        DH-1,0,10,GR,Granite, coarse grained,pink

    gave ``description='Granite'``, ``color='coarse grained'`` and lost
    ``pink``, with nothing in the run to say so. The line is now found
    (``_csv_io.read_csv_checked``), skipped with a ``ragged_row`` entry, and
    summarised by one ``ragged_row`` warning that quotes the first row.
"""

from __future__ import annotations

import io

import polars as pl
import pytest

from georag_geoparsers import (
    parse_csv_alteration,
    parse_csv_collars,
    parse_csv_geochronology,
    parse_csv_lithology,
    parse_csv_samples,
    parse_csv_structures,
    parse_csv_surveys,
)
from georag_geoparsers._csv_io import (
    DEFAULT_NULL_VALUES,
    RaggedRows,
    locate_ragged_rows,
    read_csv_checked,
)

LITHOLOGY_RAGGED = (
    "HoleID,From,To,Lithology,Description,Colour\n"
    "DH-1,0,10,GR,Granite, coarse grained,pink\n"
    "DH-1,10,20,SST,Sandstone,grey\n"
)


def _codes(result) -> list[str]:
    return [w["code"] for w in result.warnings]


def _warning(result, code):
    return next(w for w in result.warnings if w["code"] == code)


class TestTheAuditRepro:
    def test_the_shifted_row_is_not_stored(self) -> None:
        result = parse_csv_lithology(io.StringIO(LITHOLOGY_RAGGED))

        assert [r["lithology_description"] for r in result.records] == ["Sandstone"]
        # Nothing of the shifted row survives: not description='Granite',
        # not color='coarse grained'.
        stored = repr(result.records)
        assert "Granite" not in stored
        assert "coarse grained" not in stored

    def test_it_is_a_named_skip_with_the_row_and_the_counts(self) -> None:
        result = parse_csv_lithology(io.StringIO(LITHOLOGY_RAGGED))

        (skip,) = result.skipped_details
        assert skip["code"] == "ragged_row"
        assert skip["row"] == 2
        assert (skip["actual"], skip["expected"]) == (7, "6 fields")
        assert skip["raw"]["fields"] == [
            "DH-1", "0", "10", "GR", "Granite", " coarse grained", "pink",
        ]
        assert (result.total_rows, result.valid_rows, result.skipped_rows) == (2, 1, 1)

    def test_the_run_gets_one_warning_naming_the_first_row(self) -> None:
        result = parse_csv_lithology(io.StringIO(LITHOLOGY_RAGGED))

        warning = _warning(result, "ragged_row")
        assert warning["context"]["rows"] == [2]
        assert warning["context"]["expected_fields"] == 6
        assert "unquoted" in warning["detail"]
        assert "coarse grained" in warning["detail"]

    def test_quoting_the_value_fixes_it_and_nothing_is_reported(self) -> None:
        fixed = LITHOLOGY_RAGGED.replace(
            "Granite, coarse grained", '"Granite, coarse grained"',
        )
        result = parse_csv_lithology(io.StringIO(fixed))

        assert "ragged_row" not in _codes(result)
        assert result.valid_rows == 2
        assert result.records[0]["lithology_description"] == "Granite, coarse grained"
        assert result.records[0]["color"] == "pink"


class TestWhatIsNotRagged:
    def test_a_trailing_delimiter_loses_nothing_and_is_not_reported(self) -> None:
        text = (
            "HoleID,From,To,Lithology,Description,Colour\n"
            "DH-1,0,10,GR,Granite,pink,\n"
            "DH-1,10,20,SST,Sandstone,grey,,\n"
        )
        result = parse_csv_lithology(io.StringIO(text))

        assert "ragged_row" not in _codes(result)
        assert result.valid_rows == 2
        assert result.records[0]["color"] == "pink"

    def test_a_short_line_keeps_its_leading_values_in_place(self) -> None:
        text = (
            "HoleID,From,To,Lithology,Description,Colour\n"
            "DH-1,0,10,GR,Granite\n"
            "DH-1,10,20,SST,Sandstone,grey\n"
        )
        result = parse_csv_lithology(io.StringIO(text))

        assert "ragged_row" not in _codes(result)
        assert result.records[0]["lithology_description"] == "Granite"
        assert result.records[0].get("color") is None

    def test_a_clean_file_pays_nothing(self) -> None:
        df, ragged = read_csv_checked(
            "a,b,c\n1,2,3\n4,5,6\n", separator=",", null_values=DEFAULT_NULL_VALUES,
        )
        assert df.shape == (2, 3)
        assert not ragged and ragged.warnings() == []


class TestEveryParserSkipsTheRow:
    def test_collar(self) -> None:
        text = (
            "HoleID,Easting,Northing,Elevation,Azimuth,Dip,Depth\n"
            "DH-1,500000,6000000,300,90,-60,150\n"
            "DH-2,500100,6000100,310,90,-60,150, note about the hole\n"
            "DH-3,500200,6000200,320,90,-60,120\n"
        )
        result = parse_csv_collars(io.StringIO(text))

        assert [r["hole_id"] for r in result.records] == ["DH-1", "DH-3"]
        assert [s["code"] for s in result.skipped_details] == ["ragged_row"]
        assert result.skipped_details[0]["row"] == 3
        assert "ragged_row" in _codes(result)

    def test_survey(self) -> None:
        text = (
            "HoleID,Depth,Azimuth,Dip\n"
            "DH-1,0,90,-60\n"
            "DH-1,50,92,-61, check this\n"
            "DH-1,100,95,-62\n"
        )
        result = parse_csv_surveys(io.StringIO(text))

        assert [r["depth"] for r in result.records] == [0.0, 100.0]
        assert result.skipped_details[0]["code"] == "ragged_row"

    def test_structure(self) -> None:
        text = (
            "HoleID,Depth,StructureType,Alpha,Beta\n"
            "DH-1,10,fault,30,45\n"
            "DH-1,20,vein, quartz,40,50\n"
            "DH-1,30,joint,35,60\n"
        )
        result = parse_csv_structures(io.StringIO(text))

        assert [r["depth"] for r in result.records] == [10.0, 30.0]
        assert result.skipped_details[0]["code"] == "ragged_row"

    def test_samples_wide(self) -> None:
        text = (
            "HoleID,From,To,SampleID,Au_ppm\n"
            "DH-1,0,1,S1,0.5\n"
            "DH-1,1,2,S2,0.7,oops\n"
            "DH-1,2,3,S3,0.9\n"
        )
        result = parse_csv_samples(io.StringIO(text))

        assert [r["sample_id"] for r in result.records] == ["S1", "S3"]
        assert result.skipped_details[0]["code"] == "ragged_row"
        assert result.skipped_details[0]["row"] == 3

    def test_samples_long_format_is_not_pivoted_with_the_shifted_row(self) -> None:
        text = (
            "HoleID,From,To,SampleID,Element,Value,Unit\n"
            "DH-1,0,1,S1,Au,0.5,ppm\n"
            "DH-1,0,1,S1,Cu,100,ppm\n"
            "DH-1,1,2,S2,Au,0.7,ppm,oops\n"
            "DH-1,2,3,S3,Au,0.9,ppm\n"
        )
        result = parse_csv_samples(io.StringIO(text))

        assert {r["sample_id"]: r["commodity_assays"] for r in result.records} == {
            "S1": {"Au_ppm": 0.5, "Cu_ppm": 100.0},
            "S3": {"Au_ppm": 0.9},
        }
        assert [s["code"] for s in result.skipped_details] == ["ragged_row"]
        assert result.skipped_details[0]["row"] == 4
        assert result.total_rows == result.valid_rows + result.skipped_rows

    def test_geochronology(self) -> None:
        text = (
            "SampleID,Age_Ma,Method\n"
            "G1,1850,U-Pb zircon\n"
            "G2,1900,U-Pb, zircon,extra\n"
            "G3,2500,U-Pb zircon\n"
        )
        result = parse_csv_geochronology(io.StringIO(text))

        assert sorted(r["sample_id"] for r in result.records) == ["G1", "G3"]
        assert [s["code"] for s in result.skipped_details] == ["ragged_row"]

    def test_alteration_reports_it_once(self) -> None:
        text = (
            "HoleID,From,To,Alteration,Intensity\n"
            "DH-1,0,10,chlorite,strong\n"
            "DH-1,10,20,sericite, moderate,extra\n"
            "DH-1,20,30,silica,weak\n"
        )
        result = parse_csv_alteration(io.StringIO(text))

        assert [r["alteration_type"] for r in result.records] == ["chlorite", "silica"]
        assert [s["code"] for s in result.skipped_details] == ["ragged_row"]
        assert _codes(result).count("ragged_row") == 1


class TestFileLevelPassesDoNotSeeTheShiftedValues:
    def test_a_ragged_row_cannot_disqualify_a_decimal_comma_column(self) -> None:
        """Decimal-comma detection reads a whole column before any row is
        judged, and one text cell disqualifies it. The shifted row puts the
        word 'Note' in the Depth column; the row is skipped, so it must not
        take every other depth of the file down with it."""
        text = (
            "HoleID;Depth;Azimuth;Dip\n"
            "DH-1;10,5;90;-60\n"
            "DH-1;Note; with a semicolon;90;-60\n"
            "DH-1;30,5;92;-61\n"
        )
        result = parse_csv_surveys(io.StringIO(text))

        assert [r["depth"] for r in result.records] == [10.5, 30.5]
        assert "decimal_comma_detected" in _codes(result)
        assert result.skipped_details[0]["code"] == "ragged_row"


class TestWhenTheRowsCannotBeLocated:
    def test_a_field_over_the_csv_limit_is_warned_about_not_swallowed(self, monkeypatch) -> None:
        import csv

        original = csv.field_size_limit()
        csv.field_size_limit(50)
        try:
            huge = "x" * 200
            text = f"a,b\n1,{huge},extra\n"
            df, ragged = read_csv_checked(
                text, separator=",", null_values=DEFAULT_NULL_VALUES,
            )
        finally:
            csv.field_size_limit(original)

        assert df.shape == (1, 2)
        assert ragged.unlocated and not ragged.rows
        (warning,) = ragged.warnings()
        assert warning["code"] == "ragged_row"
        assert warning["context"] == {"located": False}

    def test_a_row_count_the_scan_disagrees_with_is_not_trusted(self) -> None:
        ragged = locate_ragged_rows(
            "a,b\n1,2,3\n4,5\n", separator=",", data_rows=5,
        )
        assert ragged.unlocated and not ragged.rows

    def test_other_polars_errors_are_not_mistaken_for_ragged_lines(self) -> None:
        with pytest.raises(pl.exceptions.PolarsError):
            read_csv_checked("", separator=",", null_values=DEFAULT_NULL_VALUES)

    def test_empty_ragged_rows_object_is_falsy_and_silent(self) -> None:
        empty = RaggedRows()
        assert not empty and len(empty) == 0 and empty.warnings() == []
