"""Tests for the 2026-05-23 CSV audit gaps #1 + #2.

Gap #1: detect_delimiter — Polars defaulted to comma, so semicolon/tab/pipe
CSVs collapsed into a single column. Test that detection picks the right
separator for each common shape.

Gap #2: transform_decimal_comma — _check_decimal_comma only warned. EU
exports with values like "1,5" used to arrive as text or as wrong
numbers. Test the column-aware gate: depth columns transform, mixed
columns are left alone, US-style thousand-separator columns are left
alone.
"""
from __future__ import annotations

import polars as pl
import pytest

from georag_dagster.parsers._csv_io import (
    detect_delimiter,
    transform_decimal_comma,
)


# ---------------------------------------------------------------------------
# Delimiter detection
# ---------------------------------------------------------------------------

def test_detect_delimiter_picks_comma_for_us_csv():
    csv = "hole_id,from,to\nPLS-22-08,100.5,120.0\nPLS-22-09,5.2,18.8\n"
    assert detect_delimiter(csv) == ","


def test_detect_delimiter_picks_semicolon_for_eu_csv():
    csv = "hole_id;from;to\nPLS-22-08;100,5;120,0\nPLS-22-09;5,2;18,8\n"
    assert detect_delimiter(csv) == ";"


def test_detect_delimiter_picks_tab_for_tsv():
    csv = "hole_id\tfrom\tto\nPLS-22-08\t100.5\t120.0\n"
    assert detect_delimiter(csv) == "\t"


def test_detect_delimiter_picks_pipe_for_pipe_csv():
    csv = "hole_id|from|to\nPLS-22-08|100.5|120.0\nPLS-22-09|5.2|18.8\n"
    assert detect_delimiter(csv) == "|"


def test_detect_delimiter_prefers_consistent_counts_over_raw_count():
    """Stray punctuation in cell text should not beat a more-consistent
    delimiter. Here ',' appears in the description field but ';' has
    more consistent N-per-line counts."""
    csv = (
        "hole_id;from;to;notes\n"
        "PLS-22-08;100.5;120.0;altered, broken zone\n"
        "PLS-22-09;5.2;18.8;competent rock\n"
    )
    assert detect_delimiter(csv) == ";"


def test_detect_delimiter_returns_default_on_empty():
    assert detect_delimiter("") == ","
    assert detect_delimiter("   \n  \n") == ","


def test_detect_delimiter_returns_default_on_single_column():
    # No delimiter anywhere → default
    csv = "hole_id\nPLS-22-08\nPLS-22-09\n"
    assert detect_delimiter(csv) == ","


# ---------------------------------------------------------------------------
# Decimal-comma transformation
# ---------------------------------------------------------------------------

def test_transform_decimal_comma_rewrites_pure_eu_numeric_column():
    df = pl.DataFrame({
        "hole_id": ["PLS-22-08", "PLS-22-09"],
        "from_depth": ["100,5", "5,2"],  # EU decimal-comma
        "to_depth": ["120,0", "18,8"],
    })
    out_df, transformed = transform_decimal_comma(df)
    assert set(transformed) == {"from_depth", "to_depth"}, transformed
    assert out_df["from_depth"].to_list() == ["100.5", "5.2"]
    assert out_df["to_depth"].to_list() == ["120.0", "18.8"]
    # hole_id is a text column with no decimal commas — must NOT be touched
    assert out_df["hole_id"].to_list() == ["PLS-22-08", "PLS-22-09"]


def test_transform_decimal_comma_leaves_us_thousand_separator_alone():
    """US thousand-separator like '1,234.56' must NOT be transformed —
    the period in the value disqualifies the column."""
    df = pl.DataFrame({
        "from_depth": ["1,234.56", "5,678.90"],
    })
    out_df, transformed = transform_decimal_comma(df)
    assert transformed == []
    assert out_df["from_depth"].to_list() == ["1,234.56", "5,678.90"]


def test_transform_decimal_comma_leaves_text_column_alone():
    df = pl.DataFrame({
        "comment": ["altered, broken", "competent, fresh"],
    })
    out_df, transformed = transform_decimal_comma(df)
    assert transformed == []
    assert out_df["comment"].to_list() == ["altered, broken", "competent, fresh"]


def test_transform_decimal_comma_skips_mixed_column():
    """Sample columns sometimes mix BDL tokens with numeric values.
    Per audit, those columns should be left alone for the downstream
    assay parser to handle."""
    df = pl.DataFrame({
        "Au_ppm": ["1,5", "BDL", "2,3", "<0.01"],
    })
    out_df, transformed = transform_decimal_comma(df)
    assert transformed == []
    # Original strings preserved
    assert out_df["Au_ppm"].to_list() == ["1,5", "BDL", "2,3", "<0.01"]


def test_transform_decimal_comma_handles_negatives_and_integers():
    df = pl.DataFrame({
        "elevation": ["-12,5", "100", "-1,7", "0,0"],
    })
    out_df, transformed = transform_decimal_comma(df)
    assert transformed == ["elevation"]
    assert out_df["elevation"].to_list() == ["-12.5", "100", "-1.7", "0.0"]


def test_transform_decimal_comma_requires_at_least_one_comma():
    """All-integer column has no commas → no transformation."""
    df = pl.DataFrame({
        "count": ["1", "2", "3", "4"],
    })
    out_df, transformed = transform_decimal_comma(df)
    assert transformed == []
    assert out_df["count"].to_list() == ["1", "2", "3", "4"]


def test_transform_decimal_comma_skips_already_numeric_columns():
    """Already-typed numeric columns are not Utf8, so the transform is
    a no-op for them — no error, no spurious transformation."""
    df = pl.DataFrame({
        "from_depth": [100.5, 5.2, 120.0],  # f64
        "label": ["a,b", "c,d", "e,f"],     # mixed text — won't match
    })
    out_df, transformed = transform_decimal_comma(df)
    assert transformed == []
    assert out_df["from_depth"].to_list() == [100.5, 5.2, 120.0]


def test_transform_decimal_comma_with_nulls():
    df = pl.DataFrame({
        "depth": ["1,5", None, "2,7", None, "3,1"],
    })
    out_df, transformed = transform_decimal_comma(df)
    assert transformed == ["depth"]
    # Nulls preserved
    out_vals = out_df["depth"].to_list()
    assert out_vals[0] == "1.5"
    assert out_vals[1] is None
    assert out_vals[2] == "2.7"
    assert out_vals[3] is None
    assert out_vals[4] == "3.1"
