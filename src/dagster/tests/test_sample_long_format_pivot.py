"""Regression tests for csv_sample.py — long-format auto-detect and pivot.

Sprint 2: Tests the _detect_long_format / _pivot_long_to_wide path and proves
that wide-format (Sprint 1) behaviour is unchanged (regression).

Run with:  pytest tests/test_sample_long_format_pivot.py -v
"""

from __future__ import annotations

from io import StringIO

import pytest

from georag_dagster.parsers.csv_sample import parse_csv_samples


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _warning_codes(result) -> list[str]:
    return [w.get("code") for w in result.warnings]


def _has_warning_code(result, code: str) -> bool:
    return code in _warning_codes(result)


# ---------------------------------------------------------------------------
# Long-format detection + pivot
# ---------------------------------------------------------------------------

class TestLongFormatDetectionAndPivot:
    _LONG_CSV = """\
HoleID,From,To,SampleType,element,value,unit
LEB23001,0,1,Core,Au,0.5,ppm
LEB23001,0,1,Core,Cu,120,ppm
LEB23001,1,2,Core,Au,0.8,ppm
LEB23001,1,2,Core,Cu,95,ppm
"""

    def test_long_format_produces_correct_record_count(self):
        """2 distinct (hole_id, from, to, sample_type) groups → 2 records."""
        result = parse_csv_samples(StringIO(self._LONG_CSV))
        assert result.valid_rows == 2, (
            f"expected 2 pivoted records, got {result.valid_rows}"
        )

    def test_long_format_detected_warning_emitted(self):
        result = parse_csv_samples(StringIO(self._LONG_CSV))
        assert _has_warning_code(result, "long_format_detected"), (
            "expected 'long_format_detected' warning for long-format input"
        )

    def test_long_format_commodity_assays_populated(self):
        result = parse_csv_samples(StringIO(self._LONG_CSV))
        assert result.valid_rows >= 1
        rec = result.records[0]
        assays = rec["commodity_assays"]
        # After pivot we should have Au_ppm and Cu_ppm
        assert any("Au" in k for k in assays), f"Au key missing from assays: {assays}"
        assert any("Cu" in k for k in assays), f"Cu key missing from assays: {assays}"


class TestLongFormatUnitNormalization:
    def _make_csv(self, unit1: str, unit2: str, unit3: str) -> str:
        return (
            "HoleID,From,To,SampleType,element,value,unit\n"
            f"LEB23001,0,1,Core,Au,0.5,{unit1}\n"
            f"LEB23002,0,1,Core,Au,0.3,{unit2}\n"
            f"LEB23003,0,1,Core,Au,0.9,{unit3}\n"
        )

    @pytest.mark.parametrize(
        "raw_unit, expected_key_part",
        [
            ("%",       "pct"),
            ("pct",     "pct"),
            ("percent", "pct"),
        ],
    )
    def test_unit_normalized_to_pct(self, raw_unit: str, expected_key_part: str):
        csv = (
            "HoleID,From,To,SampleType,element,value,unit\n"
            f"LEB23001,0,1,Core,Au,0.5,{raw_unit}\n"
        )
        result = parse_csv_samples(StringIO(csv))
        assert result.valid_rows >= 1
        assays = result.records[0]["commodity_assays"]
        col_names = list(assays.keys())
        assert any(expected_key_part in k for k in col_names), (
            f"expected column containing '{expected_key_part}' for unit='{raw_unit}', "
            f"got columns: {col_names}"
        )

    def test_non_canonical_unit_emits_unit_normalized_warning(self):
        csv = (
            "HoleID,From,To,SampleType,element,value,unit\n"
            "LEB23001,0,1,Core,Au,0.5,%\n"
        )
        result = parse_csv_samples(StringIO(csv))
        assert _has_warning_code(result, "unit_normalized"), (
            "expected 'unit_normalized' warning for unit='%'"
        )

    def test_canonical_unit_ppm_no_unit_normalized_warning(self):
        csv = (
            "HoleID,From,To,SampleType,element,value,unit\n"
            "LEB23001,0,1,Core,Au,0.5,ppm\n"
        )
        result = parse_csv_samples(StringIO(csv))
        # "ppm" is already canonical — no unit_normalized warning expected
        assert not _has_warning_code(result, "unit_normalized"), (
            "ppm is already canonical; should not emit unit_normalized warning"
        )


class TestLongFormatDetectionLimit:
    def test_detection_limit_column_sets_dl_flag(self):
        """Long-format with detection_limit column and value below DL.
        The DL flag should appear in commodity_assay_flags with dl_threshold from the column.
        """
        csv = (
            "HoleID,From,To,SampleType,element,value,unit,detection_limit\n"
            "LEB23001,0,1,Core,Au,0.001,ppm,0.01\n"
        )
        result = parse_csv_samples(StringIO(csv))
        assert result.valid_rows >= 1
        rec = result.records[0]
        flags = rec.get("commodity_assay_flags") or {}
        # Find the Au_ppm key in flags
        au_flag_key = next((k for k in flags if "Au" in k), None)
        assert au_flag_key is not None, f"Au flag key not found; flags: {flags}"
        assert flags[au_flag_key].get("dl_threshold") == 0.01, (
            f"expected dl_threshold=0.01 from detection_limit column, "
            f"got {flags[au_flag_key]}"
        )


class TestLongFormatMissingGroupingColumn:
    def test_missing_from_depth_returns_empty_with_error_code(self):
        """Long-format CSV missing from_depth grouping column → error, empty records."""
        csv = (
            "HoleID,To,SampleType,element,value,unit\n"
            "LEB23001,1,Core,Au,0.5,ppm\n"
        )
        result = parse_csv_samples(StringIO(csv))
        # Either: records are empty and skipped_details has the error code,
        # OR the parser falls back to wide format (no assay columns match → treated as long,
        # then fails). Either way records should be empty.
        all_codes = [d.get("code") for d in result.skipped_details]
        assert result.valid_rows == 0
        assert "long_format_missing_grouping_column" in all_codes, (
            f"expected 'long_format_missing_grouping_column' in skipped_details codes, "
            f"got: {all_codes}"
        )


class TestWideFomatRegression:
    """Prove wide-format (Sprint 1 behaviour) is unchanged."""

    _WIDE_CSV = """\
HoleID,From,To,SampleType,Au_ppm,Cu_ppm
LEB23001,0,1,Core,0.5,120
LEB23001,1,2,Core,0.8,95
"""

    def test_wide_format_parses_correctly(self):
        result = parse_csv_samples(StringIO(self._WIDE_CSV))
        assert result.valid_rows == 2

    def test_wide_format_no_long_format_detected_warning(self):
        result = parse_csv_samples(StringIO(self._WIDE_CSV))
        assert not _has_warning_code(result, "long_format_detected"), (
            "wide format input must NOT emit 'long_format_detected' warning"
        )

    def test_wide_format_commodity_assays_populated(self):
        result = parse_csv_samples(StringIO(self._WIDE_CSV))
        assert result.valid_rows >= 1
        assays = result.records[0]["commodity_assays"]
        assert "Au_ppm" in assays, f"Au_ppm missing from wide-format assays: {assays}"
        assert "Cu_ppm" in assays, f"Cu_ppm missing from wide-format assays: {assays}"


class TestAmbiguousWideVsLong:
    def test_both_assay_col_and_element_col_uses_wide_format(self):
        """If the file has both Au_ppm (assay column) AND an 'element' column,
        the spec says 'long format if no ASSAY_COLUMN_RE columns match'.
        With Au_ppm present, it stays wide — no long_format_detected warning.
        """
        csv = (
            "HoleID,From,To,SampleType,Au_ppm,element,value\n"
            "LEB23001,0,1,Core,0.5,Au,0.5\n"
        )
        result = parse_csv_samples(StringIO(csv))
        assert not _has_warning_code(result, "long_format_detected"), (
            "when assay columns (Au_ppm) are present, should use wide format"
        )
