"""Regression tests — hole_id_canonical + collision detection in parsers.

Sprint 2: Every CSV parser populates hole_id_canonical on each record and
emits a 'hole_id_canonical_collision' warning when different raw forms
canonicalize to the same value.

Covers csv_collar (primary), csv_sample (secondary), and csv_lithology
(tertiary). Also verifies clean parse (no collision warning) with all-unique IDs.

Run with:  pytest tests/test_hole_id_canonical_in_parsers.py -v
"""

from __future__ import annotations

from io import StringIO

import pytest

from georag_dagster.parsers.csv_collar import parse_csv_collars
from georag_dagster.parsers.csv_lithology import parse_csv_lithology
from georag_dagster.parsers.csv_sample import parse_csv_samples


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collision_warnings(result) -> list[dict]:
    return [
        w for w in result.warnings
        if w.get("code") == "hole_id_canonical_collision"
    ]


# ---------------------------------------------------------------------------
# CSV Collar — canonical collision detection
# ---------------------------------------------------------------------------

class TestCollarCanonicalCollision:
    _COLLISION_CSV = (
        "HoleID,Easting,Northing,Elevation,TotalDepth,HoleType,Status\n"
        "LEB-23-001,500000,5500000,450,150,Diamond,Active\n"
        "LEB_23_001,500050,5500050,455,160,Diamond,Active\n"
    )

    def test_both_records_parse_successfully(self):
        """Collision does NOT cause rows to be rejected — it emits a warning."""
        result = parse_csv_collars(StringIO(self._COLLISION_CSV))
        assert result.valid_rows == 2, (
            f"both rows should parse; got {result.valid_rows} valid, "
            f"skipped_details={result.skipped_details}"
        )

    def test_both_records_have_same_canonical(self):
        result = parse_csv_collars(StringIO(self._COLLISION_CSV))
        assert result.valid_rows == 2
        assert result.records[0]["hole_id_canonical"] == "LEB23001"
        assert result.records[1]["hole_id_canonical"] == "LEB23001"

    def test_collision_warning_emitted_exactly_once(self):
        result = parse_csv_collars(StringIO(self._COLLISION_CSV))
        collisions = _collision_warnings(result)
        assert len(collisions) == 1, (
            f"expected exactly 1 collision warning, got {len(collisions)}: {collisions}"
        )

    def test_collision_warning_has_raw_a_and_raw_b(self):
        result = parse_csv_collars(StringIO(self._COLLISION_CSV))
        collisions = _collision_warnings(result)
        assert len(collisions) >= 1
        ctx = collisions[0].get("context", {})
        assert "raw_a" in ctx, f"collision warning context missing 'raw_a': {ctx}"
        assert "raw_b" in ctx, f"collision warning context missing 'raw_b': {ctx}"

    def test_collision_warning_context_has_canonical(self):
        result = parse_csv_collars(StringIO(self._COLLISION_CSV))
        collisions = _collision_warnings(result)
        ctx = collisions[0].get("context", {})
        assert ctx.get("canonical") == "LEB23001"

    def test_no_collision_with_unique_ids(self):
        csv = (
            "HoleID,Easting,Northing,Elevation,TotalDepth,HoleType,Status\n"
            "LEB23001,500000,5500000,450,150,Diamond,Active\n"
            "LEB23002,500100,5500100,455,200,Diamond,Active\n"
            "LEB23003,500200,5500200,460,175,Diamond,Active\n"
        )
        result = parse_csv_collars(StringIO(csv))
        collisions = _collision_warnings(result)
        assert collisions == [], (
            f"no collision warnings expected for unique IDs; got: {collisions}"
        )


# ---------------------------------------------------------------------------
# CSV Collar — hole_id_canonical on each record
# ---------------------------------------------------------------------------

class TestCollarHoleIdCanonical:
    def test_canonical_on_clean_id(self):
        csv = (
            "HoleID,Easting,Northing,Elevation\n"
            "LEB-23-001,500000,5500000,450\n"
        )
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.records[0]["hole_id_canonical"] == "LEB23001"

    def test_canonical_on_id_with_underscores(self):
        csv = (
            "HoleID,Easting,Northing,Elevation\n"
            "leb_23_002,500000,5500000,450\n"
        )
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.records[0]["hole_id_canonical"] == "LEB23002"

    def test_canonical_on_already_canonical_id(self):
        csv = (
            "HoleID,Easting,Northing,Elevation\n"
            "LEB23003,500000,5500000,450\n"
        )
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.records[0]["hole_id_canonical"] == "LEB23003"


# ---------------------------------------------------------------------------
# CSV Sample — canonical collision detection fires too
# ---------------------------------------------------------------------------

class TestSampleCanonicalCollision:
    _COLLISION_CSV = (
        "HoleID,From,To,SampleType,Au_ppm\n"
        "LEB-23-001,0,1,Core,0.5\n"
        "LEB_23_001,1,2,Core,0.8\n"
    )

    def test_both_records_parse_and_have_canonical(self):
        result = parse_csv_samples(StringIO(self._COLLISION_CSV))
        assert result.valid_rows == 2
        assert result.records[0]["hole_id_canonical"] == "LEB23001"
        assert result.records[1]["hole_id_canonical"] == "LEB23001"

    def test_collision_warning_fired_in_sample_parser(self):
        result = parse_csv_samples(StringIO(self._COLLISION_CSV))
        collisions = _collision_warnings(result)
        assert len(collisions) >= 1, (
            "expected hole_id_canonical_collision warning in csv_sample parser"
        )

    def test_collision_context_has_required_keys(self):
        result = parse_csv_samples(StringIO(self._COLLISION_CSV))
        collisions = _collision_warnings(result)
        assert len(collisions) >= 1
        ctx = collisions[0].get("context", {})
        assert "raw_a" in ctx
        assert "raw_b" in ctx
        assert "canonical" in ctx


# ---------------------------------------------------------------------------
# CSV Lithology — canonical collision detection fires too
# ---------------------------------------------------------------------------

class TestLithologyCanonicalCollision:
    _COLLISION_CSV = (
        "HoleID,From,To,Lithology\n"
        "LEB-23-001,0,5,GR\n"
        "LEB_23_001,5,10,QTZ\n"
    )

    def test_collision_warning_fired_in_lithology_parser(self):
        result = parse_csv_lithology(StringIO(self._COLLISION_CSV))
        collisions = _collision_warnings(result)
        assert len(collisions) >= 1, (
            "expected hole_id_canonical_collision warning in csv_lithology parser"
        )

    def test_both_records_parse_with_correct_canonical(self):
        result = parse_csv_lithology(StringIO(self._COLLISION_CSV))
        assert result.valid_rows == 2
        assert result.records[0]["hole_id_canonical"] == "LEB23001"
        assert result.records[1]["hole_id_canonical"] == "LEB23001"
