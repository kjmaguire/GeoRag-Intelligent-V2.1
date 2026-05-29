"""Tests for the unit-ambiguity detector + its integration in csv_sample.py.

CC-01 Item 1 Slice 2 coverage:

* Wide-format detector flags bare-noble-metal columns (``Au``, ``Ag``).
* Wide-format detector flags bare-base-metal columns when value range
  contradicts the default (``Cu = 5000`` looks like ppm in a column
  defaulted to pct).
* Long-format detector flags rows where the unit cell is empty on a
  noble metal.
* Long-format detector flags minority-unit rows when an element appears
  with cross-mixed units in the same file.
* csv_sample.parse_csv_samples surfaces per-record outlier_flags
  aligned 1:1 with records and shaped for direct insertion into
  silver.review_queue.outlier_flags.
"""

from __future__ import annotations

from io import StringIO

import pytest

from georag_dagster.parsers._unit_ambiguity import (
    detect_long_format_units,
    detect_wide_format,
    merge_flags,
)
from georag_dagster.parsers.csv_sample import parse_csv_samples


# ---------------------------------------------------------------------------
# Wide-format detector
# ---------------------------------------------------------------------------

def test_wide_format_flags_bare_noble_metal_column():
    records = [
        {"commodity_assays": {"Au": 1.2, "Cu_pct": 0.5}},
    ]
    flags = detect_wide_format(["Au", "Cu_pct"], records)

    assert len(flags) == 1
    assert flags[0], "bare-Au column should have flagged"
    assert any("Au" in s for s in flags[0])
    assert all("Cu_pct" not in s for s in flags[0]), "explicit-unit column must NOT flag"


def test_wide_format_does_not_flag_explicit_unit_noble_metal():
    records = [{"commodity_assays": {"Au_ppm": 1.2}}]
    flags = detect_wide_format(["Au_ppm"], records)
    assert flags == [[]]


def test_wide_format_flags_bare_base_metal_when_value_suggests_wrong_unit():
    # Cu with no unit defaults to pct, but 5000 is way too high for pct
    # (5000% Cu is impossible) — suggests ppm.
    records = [{"commodity_assays": {"Cu": 5000.0}}]
    flags = detect_wide_format(["Cu"], records)
    assert flags[0], "value-range heuristic should trigger"
    assert any("ppm" in s for s in flags[0])


def test_wide_format_does_not_flag_bare_base_metal_when_value_consistent_with_pct():
    records = [{"commodity_assays": {"Cu": 2.5}}]
    flags = detect_wide_format(["Cu"], records)
    assert flags == [[]]


def test_wide_format_empty_records_returns_empty_list():
    assert detect_wide_format(["Au_ppm"], []) == []


def test_wide_format_no_assay_columns_returns_empty_per_record():
    records = [{"commodity_assays": {}}, {"commodity_assays": {}}]
    assert detect_wide_format([], records) == [[], []]


# ---------------------------------------------------------------------------
# Long-format detector
# ---------------------------------------------------------------------------

def test_long_format_flags_missing_unit_on_noble_metal():
    rows = [
        {"element": "Au", "value": "1.5", "unit": ""},
        {"element": "Cu", "value": "0.5", "unit": ""},  # base metal — no flag
    ]
    flags = detect_long_format_units(rows, element_col="element", unit_col="unit")

    assert flags[0], "missing unit on Au should flag"
    assert any("Au" in s for s in flags[0])
    assert flags[1] == [], "missing unit on Cu must NOT flag (base metal)"


def test_long_format_flags_minority_unit_when_cross_mixed():
    # 3 rows of Au in g/t, 1 row in oz/t — the oz/t row should be flagged.
    rows = [
        {"element": "Au", "value": "1.0", "unit": "g/t"},
        {"element": "Au", "value": "2.0", "unit": "g/t"},
        {"element": "Au", "value": "3.0", "unit": "g/t"},
        {"element": "Au", "value": "0.05", "unit": "oz/t"},
    ]
    flags = detect_long_format_units(rows, element_col="element", unit_col="unit")

    assert flags[0] == [] and flags[1] == [] and flags[2] == [], \
        "majority-unit rows must not flag"
    assert flags[3], "minority oz/t row should flag"
    assert any("cross-mixing" in s.lower() or "differs" in s.lower() for s in flags[3])


def test_long_format_no_flag_when_all_units_consistent():
    rows = [
        {"element": "Cu", "value": "0.5", "unit": "pct"},
        {"element": "Cu", "value": "1.0", "unit": "pct"},
    ]
    flags = detect_long_format_units(rows, element_col="element", unit_col="unit")
    assert flags == [[], []]


def test_long_format_handles_missing_unit_column():
    rows = [
        {"element": "Au", "value": "1.0"},
        {"element": "Cu", "value": "0.5"},
    ]
    flags = detect_long_format_units(rows, element_col="element", unit_col=None)
    # Au row: unit is empty AND noble metal → flag
    # Cu row: unit is empty but base metal → no flag
    assert flags[0], "Au with no unit column should flag"
    assert flags[1] == []


# ---------------------------------------------------------------------------
# merge_flags
# ---------------------------------------------------------------------------

def test_merge_flags_preserves_order_and_dedups():
    out = merge_flags(["a", "b"], ["b", "c"])
    assert out == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Integration via csv_sample.parse_csv_samples
# ---------------------------------------------------------------------------

def test_parse_csv_samples_surfaces_outlier_flags_for_bare_au_column():
    csv = (
        "HoleID,From,To,SampleType,Au\n"
        "PLS-20-01,10,11,Core,1.2\n"
        "PLS-20-01,11,12,Core,3.4\n"
    )
    result = parse_csv_samples(StringIO(csv))

    assert result.valid_rows == 2
    assert len(result.outlier_flags) == 2
    # Both rows should carry the bare-Au flag.
    for row_flags in result.outlier_flags:
        assert "unit_ambiguity" in row_flags
        assert any("Au" in s for s in row_flags["unit_ambiguity"])


def test_parse_csv_samples_emits_empty_flags_for_clean_explicit_unit_columns():
    csv = (
        "HoleID,From,To,SampleType,Au_ppm,Cu_pct\n"
        "PLS-20-01,10,11,Core,1.2,0.5\n"
    )
    result = parse_csv_samples(StringIO(csv))

    assert result.valid_rows == 1
    assert result.outlier_flags == [{}], (
        "clean explicit-unit row must produce an empty flags dict (signals 'no review needed')"
    )


def test_parse_csv_samples_long_format_missing_unit_flags_au_rows():
    csv = (
        "HoleID,From,To,SampleType,Element,Value,Unit\n"
        "PLS-20-01,10,11,Core,Au,1.2,\n"   # missing unit on Au
        "PLS-20-01,11,12,Core,Cu,0.5,pct\n"
    )
    result = parse_csv_samples(StringIO(csv))

    assert result.valid_rows == 2
    # The Au row should carry a unit_ambiguity flag; the Cu row should not.
    # Order of records after pivot follows group-key encounter order.
    flagged_count = sum(
        1 for f in result.outlier_flags if "unit_ambiguity" in f
    )
    assert flagged_count >= 1, (
        f"expected at least one row to carry a unit_ambiguity flag, "
        f"got: {result.outlier_flags}"
    )
