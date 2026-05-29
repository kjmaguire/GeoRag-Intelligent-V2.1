"""Regression tests for _dip_convention.py and its integration with csv_collar.py
and csv_survey.py — Sprint 1 dip sign-convention detection and normalisation.

Covers:
  - detect_dip_convention() with 5+ all-negative, 5+ all-positive, mixed, <5 samples,
    empty list, and lists containing None values.
  - normalize_dip() for each convention and the ambiguous no-op case.
  - Integration with parse_csv_collars(): down-positive dips are flipped, the
    result.dip_convention field is set, and exactly one dip_convention_normalized
    warning is emitted.
  - Integration with parse_csv_surveys(): same proof-of-wiring check.

Run with:  pytest tests/test_dip_convention.py -v
"""

from __future__ import annotations

from io import StringIO

import pytest

from georag_dagster.parsers._dip_convention import detect_dip_convention, normalize_dip
from georag_dagster.parsers.csv_collar import parse_csv_collars
from georag_dagster.parsers.csv_survey import parse_csv_surveys

# ---------------------------------------------------------------------------
# Minimal CSV helpers
# ---------------------------------------------------------------------------

_COLLAR_HEADER = "HoleID,Easting,Northing,Elevation,Azimuth,Dip"

def _collar_row(hole: str, dip: float) -> str:
    return f"{hole},495000.0,6200000.0,450.0,135.0,{dip}"


_SURVEY_HEADER = "HoleID,Depth,Azimuth,Dip"

def _survey_row(hole: str, depth: float, dip: float) -> str:
    return f"{hole},{depth},135.0,{dip}"


def _csv(*lines: str) -> StringIO:
    return StringIO("\n".join(lines))


# ---------------------------------------------------------------------------
# Unit tests: detect_dip_convention
# ---------------------------------------------------------------------------

class TestDetectDipConvention:
    def test_five_or_more_all_negative_returns_down_negative(self):
        result = detect_dip_convention([-45, -50, -60, -70, -80])
        assert result == "down_negative"

    def test_five_or_more_all_positive_returns_down_positive(self):
        result = detect_dip_convention([45, 50, 60, 70, 80])
        assert result == "down_positive"

    def test_mixed_values_returns_ambiguous(self):
        result = detect_dip_convention([-45, 50, -60, 70, 45])
        assert result == "ambiguous"

    def test_fewer_than_five_samples_returns_down_negative_default(self):
        """<5 samples → returns 'down_negative' (the safe DB default per spec)."""
        result = detect_dip_convention([-45, -50])
        assert result == "down_negative"

    def test_single_sample_returns_down_negative_default(self):
        result = detect_dip_convention([-45])
        assert result == "down_negative"

    def test_empty_list_returns_down_negative_default(self):
        result = detect_dip_convention([])
        assert result == "down_negative"

    def test_none_values_filtered_still_classifies_correctly(self):
        """None values in the input list must be filtered before classification."""
        result = detect_dip_convention([None, None, -45, -50, -60, -70, -80])
        assert result == "down_negative"

    def test_none_values_with_positives_still_classifies_down_positive(self):
        result = detect_dip_convention([None, 45, 50, 60, 70, 80])
        assert result == "down_positive"

    def test_exactly_five_negative_dips_at_boundary(self):
        result = detect_dip_convention([-20, -30, -45, -60, -80])
        assert result == "down_negative"

    def test_exactly_five_positive_dips_at_boundary(self):
        result = detect_dip_convention([20, 30, 45, 60, 80])
        assert result == "down_positive"

    def test_high_majority_negative_with_one_zero(self):
        """Zero is counted in both neg (0 in [-90,0]) and pos (0 in [0,90]).
        Six negatives, one zero — majority still negative."""
        result = detect_dip_convention([-45, -50, -60, -70, -80, -85, 0])
        assert result == "down_negative"


# ---------------------------------------------------------------------------
# Unit tests: normalize_dip
# ---------------------------------------------------------------------------

class TestNormalizeDip:
    def test_down_positive_convention_flips_sign(self):
        assert normalize_dip(45.0, "down_positive") == pytest.approx(-45.0)

    def test_down_positive_convention_flips_large_dip(self):
        assert normalize_dip(85.0, "down_positive") == pytest.approx(-85.0)

    def test_down_negative_convention_returns_unchanged(self):
        assert normalize_dip(-45.0, "down_negative") == pytest.approx(-45.0)

    def test_ambiguous_convention_returns_value_unchanged(self):
        """Do not flip when unsure — pass through as-is."""
        assert normalize_dip(45.0, "ambiguous") == pytest.approx(45.0)

    def test_zero_dip_down_positive_stays_zero(self):
        assert normalize_dip(0.0, "down_positive") == pytest.approx(0.0)

    def test_negative_dip_down_negative_unchanged(self):
        assert normalize_dip(-90.0, "down_negative") == pytest.approx(-90.0)


# ---------------------------------------------------------------------------
# Integration: parse_csv_collars + dip convention
# ---------------------------------------------------------------------------

class TestCollarDipConventionIntegration:
    def test_down_positive_dips_flipped_to_negative(self):
        """5 rows, all positive dips (down-positive convention) → all records
        must have negative dip values after parsing."""
        rows = [
            _collar_row("DH-01", 20),
            _collar_row("DH-02", 40),
            _collar_row("DH-03", 55),
            _collar_row("DH-04", 65),
            _collar_row("DH-05", 80),
        ]
        csv = _csv(_COLLAR_HEADER, *rows)
        result = parse_csv_collars(csv)
        assert result.dip_convention == "down_positive"
        assert result.valid_rows == 5
        for record in result.records:
            assert record["dip"] < 0, (
                f"Expected negative dip after normalisation, got {record['dip']}"
            )

    def test_down_positive_emits_exactly_one_convention_warning(self):
        rows = [
            _collar_row("DH-01", 20),
            _collar_row("DH-02", 40),
            _collar_row("DH-03", 55),
            _collar_row("DH-04", 65),
            _collar_row("DH-05", 80),
        ]
        csv = _csv(_COLLAR_HEADER, *rows)
        result = parse_csv_collars(csv)
        convention_warnings = [
            w for w in result.warnings if w["code"] == "dip_convention_normalized"
        ]
        assert len(convention_warnings) == 1, (
            "Expected exactly one dip_convention_normalized warning for down-positive data"
        )

    def test_down_negative_dips_unchanged_no_convention_warning(self):
        """5 rows, all negative dips → no sign flip, no convention warning."""
        rows = [
            _collar_row("DH-01", -20),
            _collar_row("DH-02", -40),
            _collar_row("DH-03", -55),
            _collar_row("DH-04", -65),
            _collar_row("DH-05", -80),
        ]
        csv = _csv(_COLLAR_HEADER, *rows)
        result = parse_csv_collars(csv)
        assert result.dip_convention == "down_negative"
        convention_warnings = [
            w for w in result.warnings if w["code"] == "dip_convention_normalized"
        ]
        assert len(convention_warnings) == 0, (
            "No dip_convention_normalized warning expected for already-negative dips"
        )
        for record in result.records:
            assert record["dip"] <= 0

    def test_mixed_dips_sets_ambiguous_convention_and_emits_warning(self):
        """Mixed dips → ambiguous convention, dip_convention_ambiguous warning,
        values passed through unchanged."""
        rows = [
            _collar_row("DH-01", -45),
            _collar_row("DH-02", 50),
            _collar_row("DH-03", -60),
            _collar_row("DH-04", 70),
            _collar_row("DH-05", 45),
        ]
        csv = _csv(_COLLAR_HEADER, *rows)
        result = parse_csv_collars(csv)
        assert result.dip_convention == "ambiguous"
        ambiguous_warnings = [
            w for w in result.warnings if w["code"] == "dip_convention_ambiguous"
        ]
        assert len(ambiguous_warnings) == 1, (
            "Expected one dip_convention_ambiguous warning for mixed dips"
        )
        # Values pass through unchanged — check the positive ones remain positive
        raw_dips = [-45, 50, -60, 70, 45]
        parsed_dips = [r["dip"] for r in result.records]
        assert parsed_dips == pytest.approx(raw_dips)


# ---------------------------------------------------------------------------
# Integration: parse_csv_surveys + dip convention
# ---------------------------------------------------------------------------

class TestSurveyDipConventionIntegration:
    def test_down_positive_survey_dips_are_normalised(self):
        """Proves csv_survey.py wires _dip_convention the same way csv_collar.py does."""
        rows = [
            _survey_row("DH-01", 50.0, 45),
            _survey_row("DH-01", 100.0, 50),
            _survey_row("DH-01", 150.0, 55),
            _survey_row("DH-01", 200.0, 60),
            _survey_row("DH-01", 250.0, 65),
        ]
        csv = _csv(_SURVEY_HEADER, *rows)
        result = parse_csv_surveys(csv)
        assert result.dip_convention == "down_positive"
        # After normalisation, dips are in [-90, 0] and pass the survey range check
        for record in result.records:
            assert record["dip"] <= 0, (
                f"Survey dip expected to be negative after normalisation, got {record['dip']}"
            )
        convention_warnings = [
            w for w in result.warnings if w["code"] == "dip_convention_normalized"
        ]
        assert len(convention_warnings) == 1
