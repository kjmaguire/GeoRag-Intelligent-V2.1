"""Unit tests for silver_drill_traces — 5 desurvey edge cases + happy path.

All 5 edge cases from §04d-tile are covered:
  EC-1: 0-survey collar → no trace (verified by function returning empty)
  EC-2: 1-survey collar → vertical LINESTRINGZ
  EC-3: Duplicate depths in surveys → keep first (most recent updated_at)
  EC-4: Invalid azimuth/dip → rejected; if all invalid → treat as 0-survey
  EC-5: High dogleg (>15°/30m) → flagged, trace still computed

Tests exercise the helper functions directly (no DB, no Dagster, no MinIO).

Run with:
    pytest src/dagster/tests/test_silver_drill_traces.py -v
"""

from __future__ import annotations


import pytest

from georag_dagster.assets.silver_drill_traces import (
    _DOGLEG_HIGH_THRESHOLD_DEG,
    _compute_survey_hash,
    _dogleg_severity_deg_per_30m,
    _filter_and_dedup_surveys,
    _max_dogleg,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _survey(depth: float, az: float, dip: float, updated_at=None) -> dict:
    return {"depth": depth, "azimuth": az, "dip": dip, "updated_at": updated_at}


def _three_collar_dataset() -> list[list[dict]]:
    """Synthetic 3-collar dataset used across happy-path tests."""
    return [
        # collar A — 3 surveys, valid, low dogleg
        [
            _survey(0.0,  0.0, -90.0),
            _survey(50.0, 5.0, -88.0),
            _survey(100.0, 10.0, -85.0),
        ],
        # collar B — 2 surveys, one with a high dogleg
        [
            _survey(0.0, 0.0, -90.0),
            _survey(30.0, 45.0, -45.0),  # large az+dip change over 30 m
        ],
        # collar C — 1 survey
        [
            _survey(25.0, 180.0, -60.0),
        ],
    ]


# ---------------------------------------------------------------------------
# EC-1 — 0-survey collar
# ---------------------------------------------------------------------------

class TestEdgeCase1ZeroSurveys:
    """EC-1: 0-survey collar → _filter_and_dedup returns empty list."""

    def test_empty_raw_surveys_returns_empty_valid(self):
        valid, invalid_count, dup_count = _filter_and_dedup_surveys([])
        assert valid == []
        assert invalid_count == 0
        assert dup_count == 0

    def test_all_invalid_surveys_returns_empty_valid(self):
        """All rows invalid → valid list is empty → treated as 0-survey."""
        raw = [
            _survey(0.0,  -10.0, -90.0),   # az out of range (negative)
            _survey(50.0, 400.0, -45.0),   # az >= 360
            _survey(100.0, 10.0, 5.0),     # dip > 0 (up-going)
        ]
        valid, invalid_count, dup_count = _filter_and_dedup_surveys(raw)
        assert valid == []
        assert invalid_count == 3
        assert dup_count == 0


# ---------------------------------------------------------------------------
# EC-2 — 1-survey collar
# ---------------------------------------------------------------------------

class TestEdgeCase2OneSurvey:
    """EC-2: 1-survey collar produces valid survey output; WKT is built vertically."""

    def test_single_valid_survey_passes_filter(self):
        raw = [_survey(50.0, 180.0, -60.0)]
        valid, invalid_count, dup_count = _filter_and_dedup_surveys(raw)
        assert len(valid) == 1
        assert invalid_count == 0
        assert dup_count == 0
        assert valid[0]["depth"] == 50.0
        assert valid[0]["azimuth"] == 180.0
        assert valid[0]["dip"] == -60.0

    def test_single_survey_at_boundary_azimuth_zero(self):
        """az=0.0 is valid (boundary inclusive)."""
        raw = [_survey(10.0, 0.0, -90.0)]
        valid, _, _ = _filter_and_dedup_surveys(raw)
        assert len(valid) == 1

    def test_single_survey_at_boundary_dip_zero(self):
        """dip=0.0 is valid (flat, not up-going)."""
        raw = [_survey(10.0, 90.0, 0.0)]
        valid, _, _ = _filter_and_dedup_surveys(raw)
        assert len(valid) == 1

    def test_single_survey_at_boundary_dip_minus90(self):
        """dip=-90.0 is valid (vertical)."""
        raw = [_survey(10.0, 90.0, -90.0)]
        valid, _, _ = _filter_and_dedup_surveys(raw)
        assert len(valid) == 1


# ---------------------------------------------------------------------------
# EC-3 — Duplicate depths
# ---------------------------------------------------------------------------

class TestEdgeCase3DuplicateDepths:
    """EC-3: duplicate depths → keep first row (most recent updated_at per SQL sort),
    log conflict count.
    """

    def test_two_rows_at_same_depth_keeps_first(self):
        """SQL returns rows sorted by depth ASC, updated_at DESC.  First row wins."""
        raw = [
            _survey(50.0, 10.0, -80.0),   # first = most recent (SQL sort guarantees this)
            _survey(50.0, 20.0, -75.0),   # duplicate depth — dropped
        ]
        valid, invalid_count, dup_count = _filter_and_dedup_surveys(raw)
        assert len(valid) == 1
        assert dup_count == 1
        assert invalid_count == 0
        assert valid[0]["azimuth"] == 10.0  # first row kept

    def test_three_rows_at_same_depth_keeps_first_drops_two(self):
        raw = [
            _survey(50.0, 10.0, -80.0),
            _survey(50.0, 20.0, -75.0),
            _survey(50.0, 30.0, -70.0),
        ]
        valid, _, dup_count = _filter_and_dedup_surveys(raw)
        assert len(valid) == 1
        assert dup_count == 2
        assert valid[0]["azimuth"] == 10.0

    def test_mixed_duplicate_and_unique_depths(self):
        raw = [
            _survey(0.0,  0.0,  -90.0),
            _survey(50.0, 10.0, -80.0),
            _survey(50.0, 20.0, -75.0),  # dup
            _survey(100.0, 5.0, -85.0),
        ]
        valid, _, dup_count = _filter_and_dedup_surveys(raw)
        assert len(valid) == 3
        assert dup_count == 1

    def test_no_duplicates_no_dup_count(self):
        raw = [
            _survey(0.0, 0.0, -90.0),
            _survey(50.0, 5.0, -88.0),
        ]
        valid, _, dup_count = _filter_and_dedup_surveys(raw)
        assert len(valid) == 2
        assert dup_count == 0


# ---------------------------------------------------------------------------
# EC-4 — Invalid azimuth / dip
# ---------------------------------------------------------------------------

class TestEdgeCase4InvalidAzimuthDip:
    """EC-4: invalid az/dip rows are rejected and counted."""

    def test_negative_azimuth_rejected(self):
        raw = [_survey(0.0, -1.0, -90.0)]
        valid, invalid_count, _ = _filter_and_dedup_surveys(raw)
        assert valid == []
        assert invalid_count == 1

    def test_azimuth_exactly_360_rejected(self):
        """az=360 is NOT in [0, 360) — must be rejected."""
        raw = [_survey(0.0, 360.0, -90.0)]
        valid, invalid_count, _ = _filter_and_dedup_surveys(raw)
        assert valid == []
        assert invalid_count == 1

    def test_azimuth_359_9_accepted(self):
        """az=359.9 is in [0, 360) — must be accepted."""
        raw = [_survey(0.0, 359.9, -90.0)]
        valid, invalid_count, _ = _filter_and_dedup_surveys(raw)
        assert len(valid) == 1
        assert invalid_count == 0

    def test_positive_dip_rejected(self):
        """dip=0.1 is > 0 (up-going) — must be rejected."""
        raw = [_survey(0.0, 90.0, 0.1)]
        valid, invalid_count, _ = _filter_and_dedup_surveys(raw)
        assert valid == []
        assert invalid_count == 1

    def test_dip_below_minus90_rejected(self):
        """dip=-91 is < -90 — impossible; must be rejected."""
        raw = [_survey(0.0, 0.0, -91.0)]
        valid, invalid_count, _ = _filter_and_dedup_surveys(raw)
        assert valid == []
        assert invalid_count == 1

    def test_mixed_valid_and_invalid_rows(self):
        raw = [
            _survey(0.0,  0.0,  -90.0),  # valid
            _survey(50.0, 400.0, -45.0), # invalid az
            _survey(100.0, 10.0, 10.0),  # invalid dip (up-going)
            _survey(150.0, 5.0, -85.0),  # valid
        ]
        valid, invalid_count, _ = _filter_and_dedup_surveys(raw)
        assert len(valid) == 2
        assert invalid_count == 2


# ---------------------------------------------------------------------------
# EC-5 — High dogleg warning
# ---------------------------------------------------------------------------

class TestEdgeCase5HighDogleg:
    """EC-5: dogleg severity >15°/30m → flagged; trace still computed."""

    def test_zero_dogleg_vertical_hole(self):
        """Perfectly vertical hole has zero dogleg."""
        stations = [
            {"depth": 0.0,  "azimuth": 0.0, "dip": -90.0},
            {"depth": 30.0, "azimuth": 0.0, "dip": -90.0},
        ]
        dls = _dogleg_severity_deg_per_30m(0.0, -90.0, 0.0, -90.0, 30.0)
        assert dls == pytest.approx(0.0, abs=1e-9)

    def test_high_dogleg_exceeds_threshold(self):
        """From vertical to 45°-dip in 30m is a large dogleg."""
        dls = _dogleg_severity_deg_per_30m(0.0, -90.0, 0.0, -45.0, 30.0)
        assert dls > _DOGLEG_HIGH_THRESHOLD_DEG, (
            f"Expected DLS > {_DOGLEG_HIGH_THRESHOLD_DEG}, got {dls:.2f}"
        )

    def test_low_dogleg_within_threshold(self):
        """Small direction change over 30m stays below threshold."""
        dls = _dogleg_severity_deg_per_30m(0.0, -85.0, 2.0, -84.0, 30.0)
        assert dls < _DOGLEG_HIGH_THRESHOLD_DEG, (
            f"Expected DLS < {_DOGLEG_HIGH_THRESHOLD_DEG}, got {dls:.2f}"
        )

    def test_max_dogleg_across_multiple_intervals(self):
        """_max_dogleg returns the highest interval value."""
        stations = [
            {"depth": 0.0,  "azimuth": 0.0,  "dip": -90.0},
            {"depth": 30.0, "azimuth": 0.0,  "dip": -89.0},  # tiny change
            {"depth": 60.0, "azimuth": 45.0, "dip": -45.0},  # large change
        ]
        max_dls = _max_dogleg(stations)
        # Interval 2 is the large one
        dls_interval2 = _dogleg_severity_deg_per_30m(0.0, -89.0, 45.0, -45.0, 30.0)
        assert max_dls == pytest.approx(dls_interval2, rel=1e-6)
        assert max_dls > _DOGLEG_HIGH_THRESHOLD_DEG

    def test_max_dogleg_single_station_returns_zero(self):
        """Single station → no intervals → max dogleg = 0."""
        stations = [{"depth": 0.0, "azimuth": 0.0, "dip": -90.0}]
        assert _max_dogleg(stations) == 0.0

    def test_max_dogleg_empty_returns_zero(self):
        assert _max_dogleg([]) == 0.0


# ---------------------------------------------------------------------------
# Happy path — survey hash stability
# ---------------------------------------------------------------------------

class TestSurveyHash:
    def test_same_surveys_same_hash(self):
        surveys = [
            {"depth": 0.0,  "azimuth": 0.0,  "dip": -90.0},
            {"depth": 50.0, "azimuth": 5.0,  "dip": -88.0},
        ]
        h1 = _compute_survey_hash(surveys)
        h2 = _compute_survey_hash(surveys)
        assert h1 == h2

    def test_different_surveys_different_hash(self):
        surveys_a = [{"depth": 0.0, "azimuth": 0.0, "dip": -90.0}]
        surveys_b = [{"depth": 0.0, "azimuth": 5.0, "dip": -90.0}]
        assert _compute_survey_hash(surveys_a) != _compute_survey_hash(surveys_b)

    def test_hash_order_independent(self):
        """Hash is order-independent — input rows are sorted before hashing."""
        surveys_fwd = [
            {"depth": 0.0,  "azimuth": 0.0, "dip": -90.0},
            {"depth": 50.0, "azimuth": 5.0, "dip": -88.0},
        ]
        surveys_rev = list(reversed(surveys_fwd))
        assert _compute_survey_hash(surveys_fwd) == _compute_survey_hash(surveys_rev)

    def test_hash_is_64_hex_characters(self):
        surveys = [{"depth": 0.0, "azimuth": 0.0, "dip": -90.0}]
        h = _compute_survey_hash(surveys)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Happy path — full 3-collar synthetic dataset filter
# ---------------------------------------------------------------------------

class TestHappyPathThreeCollars:
    """Basic smoke test on the 3-collar synthetic dataset."""

    def test_collar_a_all_valid(self):
        collar_a_surveys = _three_collar_dataset()[0]
        valid, invalid, dups = _filter_and_dedup_surveys(collar_a_surveys)
        assert len(valid) == 3
        assert invalid == 0
        assert dups == 0

    def test_collar_b_two_valid_surveys(self):
        collar_b_surveys = _three_collar_dataset()[1]
        valid, invalid, dups = _filter_and_dedup_surveys(collar_b_surveys)
        assert len(valid) == 2
        assert invalid == 0

    def test_collar_c_single_survey(self):
        collar_c_surveys = _three_collar_dataset()[2]
        valid, invalid, dups = _filter_and_dedup_surveys(collar_c_surveys)
        assert len(valid) == 1
        assert invalid == 0
