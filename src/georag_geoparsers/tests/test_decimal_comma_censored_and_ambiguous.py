"""Decimal-comma columns that hold censored cells, and ones that cannot be told.

WHY THIS FILE EXISTS (audit findings 10 and 17)
    ``transform_decimal_comma`` rewrote a column only when EVERY sampled cell
    was a decimal-comma number or an integer. A single below-detection cell
    (``<0,005``) is neither, so it disqualified the column - and
    ``csv_sample._parse_assay_value`` cannot read ``0,52`` - which meant an
    assay column from a European lab lost every value the moment it carried
    one censored result. Real certificates carry hundreds.

    The opposite fault sat beside it: a column of ``1,250``, ``2,500`` and
    ``12,000`` matches the decimal-comma shape, so it was silently read as
    1.25, 2.5 and 12.0 - but it is exactly what a US thousands-separated
    column looks like (1250, 2500, 12000), a thousand-fold difference with no
    warning.
"""

from __future__ import annotations

import io

import polars as pl

from georag_geoparsers._csv_io import transform_decimal_comma
from georag_geoparsers.csv_sample import parse_csv_samples


def _col(values: list[str | None], name: str = "Au_ppm") -> pl.DataFrame:
    return pl.DataFrame({name: values}, schema={name: pl.Utf8})


class TestCensoredCellsDoNotDisqualifyTheColumn:
    def test_a_below_detection_cell_with_a_comma_decimal(self) -> None:
        df, transformed = transform_decimal_comma(
            _col(["0,52", "<0,005", "1,5", "12"]),
        )

        assert list(transformed) == ["Au_ppm"]
        assert df["Au_ppm"].to_list() == ["0.52", "<0.005", "1.5", "12"]

    def test_over_detection_and_spaced_prefixes(self) -> None:
        df, transformed = transform_decimal_comma(_col(["> 10,5", "< 0,01", "3,2"]))

        assert list(transformed) == ["Au_ppm"]
        assert df["Au_ppm"].to_list() == ["> 10.5", "< 0.01", "3.2"]

    def test_censored_cells_alone_are_enough_evidence(self) -> None:
        df, transformed = transform_decimal_comma(_col(["<0,005", "<0,01", "5"]))

        assert list(transformed) == ["Au_ppm"]
        assert df["Au_ppm"].to_list() == ["<0.005", "<0.01", "5"]

    def test_non_result_tokens_among_the_numbers(self) -> None:
        df, transformed = transform_decimal_comma(
            _col(["0,52", "BDL", "NS", "<DL", "IS", "0,7"]),
        )

        assert list(transformed) == ["Au_ppm"]
        assert df["Au_ppm"].to_list() == ["0.52", "BDL", "NS", "<DL", "IS", "0.7"]

    def test_a_point_decimal_beside_a_comma_decimal_is_still_refused(self) -> None:
        """Two conventions in one column: nothing may be guessed."""
        df, transformed = transform_decimal_comma(_col(["0,52", "<0.01", "1,5"]))

        assert list(transformed) == []
        assert df["Au_ppm"].to_list() == ["0,52", "<0.01", "1,5"]

    def test_free_text_still_disqualifies(self) -> None:
        _df, transformed = transform_decimal_comma(
            _col(["0,52", "sample lost", "1,5"]),
        )
        assert list(transformed) == []

    def test_only_the_cells_that_are_decimal_commas_are_rewritten(self) -> None:
        """The sample is the first 500 rows. A later cell the sample never
        reached must not have its comma blindly turned into a point."""
        cells = ["0,5"] * 3 + ["n/a, see note"]
        df, transformed = transform_decimal_comma(_col(cells), sample_size=3)

        assert list(transformed) == ["Au_ppm"]
        assert df["Au_ppm"].to_list() == ["0.5", "0.5", "0.5", "n/a, see note"]


class TestAThousandsShapedColumnIsAmbiguous:
    def test_every_comma_group_of_three_digits_is_not_converted(self) -> None:
        df, transformed = transform_decimal_comma(
            _col(["1,250", "2,500", "12,000", "7"], name="Depth"),
        )

        assert list(transformed) == []
        assert transformed.ambiguous == {"Depth": ["1,250", "2,500", "12,000"]}
        assert df["Depth"].to_list() == ["1,250", "2,500", "12,000", "7"]

    def test_the_warning_names_the_column_and_both_readings(self) -> None:
        _df, transformed = transform_decimal_comma(
            _col(["1,250", "2,500"], name="Depth"),
        )

        (warning,) = transformed.ambiguity_warnings()
        assert warning["code"] == "decimal_comma_ambiguous"
        assert warning["context"]["columns"] == ["Depth"]
        assert "1.25" in warning["detail"] and "1250" in warning["detail"]
        assert "thousand-fold" in warning["detail"]

    def test_one_cell_that_cannot_be_a_thousands_group_settles_it(self) -> None:
        for decisive in ("0,750", "12,5", "3,14"):
            df, transformed = transform_decimal_comma(
                _col(["1,250", decisive], name="Depth"),
            )
            assert list(transformed) == ["Depth"], decisive
            assert transformed.ambiguous == {}
            assert df["Depth"].to_list()[0] == "1.250"

    def test_a_clean_decimal_comma_column_is_unaffected(self) -> None:
        df, transformed = transform_decimal_comma(_col(["100,5", "5,2"], name="Depth"))

        assert list(transformed) == ["Depth"]
        assert transformed.ambiguity_warnings() == []
        assert df["Depth"].to_list() == ["100.5", "5.2"]

    def test_no_commas_no_ambiguity(self) -> None:
        _df, transformed = transform_decimal_comma(_col(["1", "2"], name="Depth"))
        assert transformed.ambiguous == {} and transformed.ambiguity_warnings() == []


class TestEndToEndThroughTheSampleParser:
    CERTIFICATE = (
        "HoleID;From;To;SampleID;Au_ppm;Cu_ppm\n"
        "DH-1;0;1;S1;0,52;<0,005\n"
        "DH-1;1;2;S2;<0,005;1,5\n"
        "DH-1;2;3;S3;1,5;12\n"
    )

    def test_the_certificate_keeps_every_value(self) -> None:
        result = parse_csv_samples(io.StringIO(self.CERTIFICATE))

        by_sample = {r["sample_id"]: r for r in result.records}
        assert by_sample["S1"]["commodity_assays"]["Au_ppm"] == 0.52
        assert by_sample["S3"]["commodity_assays"]["Au_ppm"] == 1.5
        # The censored cells are half the detection limit, flagged - read
        # from "<0,005", not lost.
        assert by_sample["S2"]["commodity_assays"]["Au_ppm"] == 0.0025
        assert by_sample["S2"]["commodity_assay_flags"]["Au_ppm"]["dl_threshold"] == 0.005
        assert by_sample["S1"]["commodity_assay_flags"]["Cu_ppm"]["dl_threshold"] == 0.005
        assert "assay_unparseable" not in {w["code"] for w in result.warnings}

    def test_long_format_values_with_a_censored_cell(self) -> None:
        text = (
            "HoleID;From;To;SampleID;Element;Value;Unit\n"
            "DH-1;0;1;S1;Au;0,52;ppm\n"
            "DH-1;1;2;S2;Au;<0,005;ppm\n"
        )
        result = parse_csv_samples(io.StringIO(text))

        by_sample = {r["sample_id"]: r for r in result.records}
        assert by_sample["S1"]["commodity_assays"]["Au_ppm"] == 0.52
        assert by_sample["S2"]["commodity_assay_flags"]["Au_ppm"]["dl_threshold"] == 0.005

    def test_an_ambiguous_depth_column_is_rejected_loudly_not_read_as_decimals(self) -> None:
        text = (
            "HoleID;From;To;SampleID;Au_ppm\n"
            "DH-1;1,250;2,500;S1;0,5\n"
            "DH-1;2,500;3,750;S2;0,7\n"
        )
        result = parse_csv_samples(io.StringIO(text))

        codes = {w["code"] for w in result.warnings}
        assert "decimal_comma_ambiguous" in codes
        # Not stored as 1.25 m: the rows are refused with the raw text shown.
        assert result.valid_rows == 0
        assert {s["code"] for s in result.skipped_details} == {"numeric_cast_failed"}
        assert "1,250" in result.skipped_details[0]["reason"]
