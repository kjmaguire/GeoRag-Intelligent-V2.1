"""Tests for the XLSX sheet-type classifier (2026-05-23 XLSX audit gap #1).

The classifier picks one of collar/survey/lithology/sample/unknown for
a given header row. Used by silver_xlsx to auto-dispatch each sheet of
a multi-sheet workbook to the right CSV parser.
"""
from __future__ import annotations


from georag_dagster.parsers._sheet_classifier import (
    classify_sheet_type,
)


# ---------------------------------------------------------------------------
# Happy paths — each sheet type with canonical headers
# ---------------------------------------------------------------------------

def test_classify_collar_canonical_headers():
    headers = ["HoleID", "Easting", "Northing", "Elevation", "TotalDepth", "Azimuth", "Dip"]
    sheet_type, confidence = classify_sheet_type(headers)
    assert sheet_type == "collar"
    assert confidence == 1.0


def test_classify_survey_canonical_headers():
    headers = ["HoleID", "Depth", "Azimuth", "Dip", "Method"]
    sheet_type, confidence = classify_sheet_type(headers)
    assert sheet_type == "survey"
    assert confidence == 1.0


def test_classify_lithology_canonical_headers():
    headers = ["HoleID", "From", "To", "Lithology", "Description", "RQD"]
    sheet_type, confidence = classify_sheet_type(headers)
    assert sheet_type == "lithology"
    assert confidence == 1.0


def test_classify_sample_canonical_headers():
    headers = ["HoleID", "From", "To", "SampleType", "LabID", "Au_ppm", "Cu_pct"]
    sheet_type, confidence = classify_sheet_type(headers)
    assert sheet_type == "sample"
    assert confidence == 1.0


# ---------------------------------------------------------------------------
# Alias variants — non-canonical but in the alias lists
# ---------------------------------------------------------------------------

def test_classify_collar_with_alias_variants():
    headers = ["DH_ID", "UTM_E", "UTM_N", "RL", "TD"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "collar"


def test_classify_survey_with_acid_test_method():
    headers = ["Hole_ID", "DEPTH", "AZI", "DIP", "Instrument"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "survey"


def test_classify_lithology_with_short_aliases():
    headers = ["HoleID", "FromDepth", "ToDepth", "LithCode", "GrainSize"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "lithology"


# ---------------------------------------------------------------------------
# Hard discriminators — type-unique fields lock the classification
# ---------------------------------------------------------------------------

def test_lithology_locked_by_lithology_code_discriminator():
    """Sheet that lacks one required field but has the unique
    lithology_code discriminator should still classify as lithology."""
    # Missing 'to_depth' (3/4 required), has unique LithCode
    headers = ["HoleID", "From", "LithCode", "Description"]
    sheet_type, confidence = classify_sheet_type(headers)
    assert sheet_type == "lithology"
    assert 0.5 <= confidence < 1.0


def test_sample_locked_by_sample_type_discriminator():
    headers = ["HoleID", "From", "SampleType"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "sample"


def test_survey_locked_by_survey_method_discriminator():
    headers = ["HoleID", "Depth", "Azimuth", "Method"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "survey"


# ---------------------------------------------------------------------------
# Unknown / ambiguous
# ---------------------------------------------------------------------------

def test_unknown_for_empty_headers():
    assert classify_sheet_type([]) == ("unknown", 0.0)


def test_unknown_for_random_business_columns():
    headers = ["Invoice", "Date", "Amount", "Customer", "Notes"]
    sheet_type, confidence = classify_sheet_type(headers)
    assert sheet_type == "unknown"
    assert confidence == 0.0


def test_unknown_for_single_hole_id_only():
    """Just hole_id alone shouldn't classify as anything — every type
    has hole_id but it's not enough signal on its own."""
    headers = ["HoleID"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "unknown"


def test_unknown_when_coverage_below_threshold():
    """Two-of-four collar required fields = 50% coverage, below the
    0.75 default → unknown."""
    headers = ["HoleID", "Easting"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "unknown"


# ---------------------------------------------------------------------------
# Case + whitespace tolerance
# ---------------------------------------------------------------------------

def test_case_insensitive_classification():
    headers = ["holeid", "easting", "northing", "elevation"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "collar"


def test_strips_whitespace_in_headers():
    headers = ["  HoleID  ", " Easting ", "Northing", "Elevation"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "collar"


# ---------------------------------------------------------------------------
# Discrimination between types that share hole_id + depth pair
# ---------------------------------------------------------------------------

def test_lithology_beats_sample_when_lithology_code_present():
    """Lithology + sample both have hole_id + from + to. Lithology has
    lithology_code which is its hard discriminator → lithology wins."""
    headers = ["HoleID", "From", "To", "LithCode", "Description"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "lithology"


def test_sample_beats_lithology_when_sample_type_present():
    """Mirror case: sample's hard discriminator is sample_type."""
    headers = ["HoleID", "From", "To", "SampleType", "LabID"]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "sample"


def test_threshold_override_lets_partial_matches_through():
    """A caller that wants to be liberal can lower the threshold."""
    headers = ["HoleID", "Easting"]  # 2/4 collar required
    sheet_type, confidence = classify_sheet_type(headers, min_required_coverage=0.5)
    assert sheet_type == "collar"
    assert confidence == 0.5


def test_classifier_handles_none_in_headers():
    """Polars sometimes returns None for blank header cells. Don't crash."""
    headers = ["HoleID", "Easting", None, "Northing", "Elevation", ""]
    sheet_type, _ = classify_sheet_type(headers)
    assert sheet_type == "collar"
