"""Regression tests for csv_sample.py — Sprint 1 below-detection and QA/QC fixes.

Covers:
  - Plain numeric assay parsing (unchanged path)
  - "<0.01" and "< 0.01" half-DL substitution (row KEPT, value = half threshold)
  - BDL literals: "BDL", "bdl", "<DL", "<LOD", "LOD" — row kept, value None
  - Unparseable strings like "NS"/"NR" — row kept, flag set
  - Regression: pre-Sprint-1, a "<0.01" row was DROPPED (valid=0, skipped=1).
    Sprint 1 fix keeps it (valid=1, skipped=0).
  - QAQC auto-detection by sample_id prefix: STD-*, BLK-*, DUP-*
  - Explicit qaqc_type in CSV is preserved and does NOT trigger detection warning.
  - sample_id with no prefix match leaves qaqc_type as inferred "Primary" (not None).

Run with:  pytest tests/test_csv_sample_assay_dl.py -v
"""

from __future__ import annotations

from io import StringIO

import pytest

from georag_dagster.parsers.csv_sample import (
    _parse_assay_value,
    _detect_qaqc_type,
    parse_csv_samples,
)

# ---------------------------------------------------------------------------
# Minimal valid CSV header / row builder
# ---------------------------------------------------------------------------

_BASE_HEADER = "HoleID,From,To,SampleType,Au_ppm"
_BASE_ROW = "PLS-20-01,270.0,271.5,Core,{au}"

_SAMPLE_ID_HEADER = "HoleID,From,To,SampleType,SampleID,Au_ppm"
_SAMPLE_ID_ROW = "PLS-20-01,270.0,271.5,Core,{sid},{au}"

_QAQC_COL_HEADER = "HoleID,From,To,SampleType,SampleID,QAQC,Au_ppm"
_QAQC_COL_ROW = "PLS-20-01,270.0,271.5,Core,{sid},{qaqc},{au}"


def _csv(header: str, *rows: str) -> StringIO:
    """Assemble a minimal StringIO CSV from header + row strings."""
    return StringIO("\n".join([header, *rows]))


# ---------------------------------------------------------------------------
# Unit tests: _parse_assay_value
# ---------------------------------------------------------------------------

class TestParseAssayValueUnit:
    """Direct unit tests of the _parse_assay_value helper."""

    def test_plain_numeric_returns_float_no_flags(self):
        value, flags = _parse_assay_value("0.42")
        assert value == pytest.approx(0.42)
        assert flags is None

    def test_plain_integer_string(self):
        value, flags = _parse_assay_value("185")
        assert value == pytest.approx(185.0)
        assert flags is None

    def test_none_input_returns_none_none(self):
        value, flags = _parse_assay_value(None)
        assert value is None
        assert flags is None

    def test_empty_string_returns_none_none(self):
        value, flags = _parse_assay_value("")
        assert value is None
        assert flags is None

    @pytest.mark.parametrize("raw", ["<0.01", "< 0.01", "<0.001", "< 0.001"])
    def test_below_detection_with_threshold_returns_half_dl(self, raw: str):
        value, flags = _parse_assay_value(raw)
        stripped = raw.replace(" ", "")
        threshold = float(stripped[1:])
        assert value == pytest.approx(threshold / 2.0)
        assert flags is not None
        assert flags["dl_flag"] is True
        assert flags["dl_threshold"] == pytest.approx(threshold)
        assert flags["substitution"] == "half_dl"
        assert flags["original"] == raw.strip()

    @pytest.mark.parametrize("raw", ["BDL", "bdl", "<DL", "<LOD", "LOD"])
    def test_bdl_literals_return_none_value_with_dl_flag(self, raw: str):
        value, flags = _parse_assay_value(raw)
        assert value is None, f"Expected value=None for BDL literal '{raw}', got {value}"
        assert flags is not None
        assert flags["dl_flag"] is True
        assert flags["dl_threshold"] is None
        assert flags["substitution"] == "null"
        assert flags["original"] == raw

    @pytest.mark.parametrize("raw", ["NS", "NR", "n.d.", "---", "trace"])
    def test_unparseable_non_dl_strings_set_unparseable_flag(self, raw: str):
        value, flags = _parse_assay_value(raw)
        assert value is None
        assert flags is not None
        assert flags.get("unparseable") is True
        assert flags["original"] == raw


# ---------------------------------------------------------------------------
# Integration tests: parse_csv_samples
# ---------------------------------------------------------------------------

class TestSampleParserPlainNumeric:
    def test_plain_numeric_assay_stored_in_commodity_assays(self):
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au="0.42"))
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1
        assert result.skipped_rows == 0
        record = result.records[0]
        assert record["commodity_assays"]["Au_ppm"] == pytest.approx(0.42)
        assert record["commodity_assay_flags"] is None


class TestSampleParserBelowDetection:
    def test_below_detection_numeric_threshold_row_kept(self):
        """Regression: pre-Sprint-1 this returned valid=0, skipped=1."""
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au="<0.01"))
        result = parse_csv_samples(csv)
        # The critical regression assertion:
        assert result.valid_rows == 1 and result.skipped_rows == 0, (
            "Regression: pre-Sprint-1 a '<0.01' row was dropped (valid=0, skipped=1). "
            "Sprint 1 fix must keep the row."
        )

    def test_below_detection_stores_half_dl_in_commodity_assays(self):
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au="<0.01"))
        result = parse_csv_samples(csv)
        record = result.records[0]
        assert record["commodity_assays"]["Au_ppm"] == pytest.approx(0.005)

    def test_below_detection_flags_set_correctly(self):
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au="<0.01"))
        result = parse_csv_samples(csv)
        record = result.records[0]
        flags = record["commodity_assay_flags"]["Au_ppm"]
        assert flags["dl_flag"] is True
        assert flags["dl_threshold"] == pytest.approx(0.01)
        assert flags["original"] == "<0.01"
        assert flags["substitution"] == "half_dl"

    def test_below_detection_tolerated_whitespace(self):
        """'< 0.01' with interior space must parse identically to '<0.01'."""
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au="< 0.01"))
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1
        record = result.records[0]
        assert record["commodity_assays"]["Au_ppm"] == pytest.approx(0.005)
        flags = record["commodity_assay_flags"]["Au_ppm"]
        assert flags["dl_flag"] is True
        assert flags["dl_threshold"] == pytest.approx(0.01)
        assert flags["substitution"] == "half_dl"


class TestSampleParserBDLLiterals:
    @pytest.mark.parametrize("raw_bdl", ["BDL", "bdl", "<DL", "<LOD", "LOD"])
    def test_bdl_literal_row_is_kept(self, raw_bdl: str):
        """Regression: pre-Sprint-1 BDL rows were treated as null and caused issues.
        Row must be kept: valid=1, skipped=0."""
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au=raw_bdl))
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1
        assert result.skipped_rows == 0

    @pytest.mark.parametrize("raw_bdl", ["BDL", "bdl", "<DL", "<LOD", "LOD"])
    def test_bdl_literal_key_absent_from_commodity_assays(self, raw_bdl: str):
        """BDL with unknown threshold: value is None, key must NOT be in commodity_assays."""
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au=raw_bdl))
        result = parse_csv_samples(csv)
        record = result.records[0]
        assert "Au_ppm" not in record["commodity_assays"], (
            f"BDL literal '{raw_bdl}' must NOT place a key in commodity_assays"
        )

    @pytest.mark.parametrize("raw_bdl", ["BDL", "bdl", "<DL", "<LOD", "LOD"])
    def test_bdl_literal_flags_set_correctly(self, raw_bdl: str):
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au=raw_bdl))
        result = parse_csv_samples(csv)
        record = result.records[0]
        assert record["commodity_assay_flags"] is not None
        flags = record["commodity_assay_flags"]["Au_ppm"]
        assert flags["dl_flag"] is True
        assert flags["dl_threshold"] is None
        assert flags["substitution"] == "null"


class TestSampleParserUnparseable:
    @pytest.mark.parametrize("raw", ["NS", "NR"])
    def test_unparseable_non_dl_row_is_kept(self, raw: str):
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au=raw))
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1, (
            f"Unparseable assay '{raw}' must NOT cause row rejection"
        )
        assert result.skipped_rows == 0

    @pytest.mark.parametrize("raw", ["NS", "NR"])
    def test_unparseable_sets_unparseable_flag(self, raw: str):
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au=raw))
        result = parse_csv_samples(csv)
        record = result.records[0]
        assert record["commodity_assay_flags"] is not None
        flags = record["commodity_assay_flags"]["Au_ppm"]
        assert flags.get("unparseable") is True

    @pytest.mark.parametrize("raw", ["NS", "NR"])
    def test_unparseable_original_preserved_in_flag(self, raw: str):
        csv = _csv(_BASE_HEADER, _BASE_ROW.format(au=raw))
        result = parse_csv_samples(csv)
        record = result.records[0]
        flags = record["commodity_assay_flags"]["Au_ppm"]
        assert flags["original"] == raw


# ---------------------------------------------------------------------------
# Unit tests: _detect_qaqc_type
# ---------------------------------------------------------------------------

class TestDetectQaqcTypeUnit:
    def test_existing_value_is_preserved(self):
        assert _detect_qaqc_type("STD-OREAS-45e", "Standard") == "Standard"
        assert _detect_qaqc_type("LEB-23-0042", "Duplicate") == "Duplicate"

    def test_std_prefix_detected(self):
        assert _detect_qaqc_type("STD-OREAS-45e", None) == "Standard"

    def test_blank_prefix_detected(self):
        assert _detect_qaqc_type("BLK-01", None) == "Blank"

    def test_dup_prefix_detected(self):
        assert _detect_qaqc_type("DUP-23", None) == "Duplicate"

    def test_no_prefix_match_returns_none(self):
        """No prefix match → returns None. Caller promotes to Primary."""
        assert _detect_qaqc_type("LEB-23-0042", None) is None

    def test_none_sample_id_returns_none(self):
        assert _detect_qaqc_type(None, None) is None

    def test_oreas_prefix_detected_as_standard(self):
        assert _detect_qaqc_type("OREAS-45e", None) == "Standard"

    def test_crm_prefix_detected_as_standard(self):
        assert _detect_qaqc_type("CRM-001", None) == "Standard"


# ---------------------------------------------------------------------------
# Integration: QAQC detection by sample_id prefix (no qaqc_type column)
# ---------------------------------------------------------------------------

class TestQaqcPrefixDetectionIntegration:
    def test_std_prefix_sets_qaqc_standard_and_emits_warning(self):
        csv = _csv(
            _SAMPLE_ID_HEADER,
            _SAMPLE_ID_ROW.format(sid="STD-OREAS-45e", au="500.0"),
        )
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1
        record = result.records[0]
        assert record["qaqc_type"] == "Standard"
        codes = [w["code"] for w in result.warnings]
        assert "qaqc_detected_by_prefix" in codes, (
            "Expected a qaqc_detected_by_prefix warning when STD- prefix found"
        )

    def test_blk_prefix_sets_qaqc_blank(self):
        csv = _csv(
            _SAMPLE_ID_HEADER,
            _SAMPLE_ID_ROW.format(sid="BLK-01", au="0.0"),
        )
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1
        assert result.records[0]["qaqc_type"] == "Blank"

    def test_dup_prefix_sets_qaqc_duplicate(self):
        csv = _csv(
            _SAMPLE_ID_HEADER,
            _SAMPLE_ID_ROW.format(sid="DUP-23", au="185.0"),
        )
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1
        assert result.records[0]["qaqc_type"] == "Duplicate"

    def test_no_prefix_match_defaults_to_primary(self):
        """sample_id with no recognised prefix → qaqc_type must be 'Primary'."""
        csv = _csv(
            _SAMPLE_ID_HEADER,
            _SAMPLE_ID_ROW.format(sid="LEB-23-0042", au="42.0"),
        )
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1
        # qaqc_type should be Primary (not None)
        assert result.records[0]["qaqc_type"] == "Primary"

    def test_explicit_qaqc_col_preserved_no_detection_warning(self):
        """Explicit QAQC='Standard' on a non-STD sample_id must be kept as-is,
        and no qaqc_detected_by_prefix warning should fire."""
        csv = _csv(
            _QAQC_COL_HEADER,
            _QAQC_COL_ROW.format(sid="LEB-23-0042", qaqc="Standard", au="500.0"),
        )
        result = parse_csv_samples(csv)
        assert result.valid_rows == 1
        record = result.records[0]
        assert record["qaqc_type"] == "Standard"
        detection_warnings = [
            w for w in result.warnings if w["code"] == "qaqc_detected_by_prefix"
        ]
        assert len(detection_warnings) == 0, (
            "No detection warning should fire when qaqc_type is explicitly in the CSV"
        )
