"""Regression tests for _hole_id.py — Sprint 2 canonicalization helpers.

Covers canonicalize(), fuzzy_match(), and suggest_collisions().

Run with:  pytest tests/test_hole_id_canonicalization.py -v
"""

from __future__ import annotations

import pytest

from georag_dagster.parsers._hole_id import (
    canonicalize,
    fuzzy_match,
    suggest_collisions,
)


# ---------------------------------------------------------------------------
# canonicalize()
# ---------------------------------------------------------------------------

class TestCanonicalize:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("LEB-23-001",    "LEB23001"),
            ("leb_23_001",    "LEB23001"),
            ("  LEB 23/001  ", "LEB23001"),
            ("LEB.23.001",    "LEB23001"),
            ("LEB23001",      "LEB23001"),   # idempotent
            ("Leb-23-001",    "LEB23001"),   # mixed case
            ("",              None),
            ("   ",           None),
            (None,            None),
        ],
    )
    def test_canonicalize_parametrized(self, raw, expected):
        assert canonicalize(raw) == expected

    def test_only_separator_chars_returns_none(self):
        # A string of only separator chars should reduce to None after removal
        assert canonicalize("---") is None

    def test_numeric_only_id(self):
        # Pure numbers are valid hole IDs in some systems
        result = canonicalize("001")
        assert result == "001"

    def test_already_canonical_is_stable(self):
        value = "LEB23001"
        assert canonicalize(value) == value


# ---------------------------------------------------------------------------
# fuzzy_match()
# ---------------------------------------------------------------------------

class TestFuzzyMatch:
    def test_exact_match(self):
        result = fuzzy_match("LEB23001", ["LEB23001", "LEB23002"])
        assert result == "LEB23001"

    def test_close_match_above_threshold(self):
        # Single-char diff: LEB23001 vs LEB23002 — ratio should be ~88 (above 80)
        result = fuzzy_match("LEB23001", ["LEB23002"], threshold=80.0)
        assert result == "LEB23002"

    def test_no_match_below_threshold(self):
        # Completely different strings — should score far below 50
        result = fuzzy_match("LEB23001", ["ABC99999"], threshold=50.0)
        assert result is None

    def test_empty_candidates(self):
        result = fuzzy_match("LEB23001", [])
        assert result is None

    def test_tie_break_returns_first(self):
        # LEB23002 and LEB23003 both differ from LEB23001 by exactly one char —
        # same score; first candidate in list should win.
        result = fuzzy_match("LEB23001", ["LEB23002", "LEB23003"])
        assert result == "LEB23002", (
            "ties must be broken by list order (first match wins)"
        )

    def test_default_threshold_rejects_noise(self):
        # Completely unrelated string must be rejected at default threshold 85.0
        result = fuzzy_match("LEB23001", ["ZZZ99999"])
        assert result is None

    def test_exact_match_in_longer_list(self):
        candidates = ["AAA11111", "LEB23001", "ZZZ99999"]
        result = fuzzy_match("LEB23001", candidates)
        assert result == "LEB23001"


# ---------------------------------------------------------------------------
# suggest_collisions()
# ---------------------------------------------------------------------------

class TestSuggestCollisions:
    def test_no_collisions_returns_empty(self):
        result = suggest_collisions(["LEB-23-001", "LEB-23-002"])
        assert result == []

    def test_one_collision_pair(self):
        # Two different raw forms that canonicalize to the same value
        ids = ["LEB-23-001", "LEB23001"]
        result = suggest_collisions(ids)
        assert len(result) >= 1, "expected at least one collision dict"
        assert all(r["canonical"] == "LEB23001" for r in result)

    def test_three_raw_forms_same_canonical(self):
        ids = ["LEB-23-001", "LEB23001", "LEB_23_001"]
        result = suggest_collisions(ids)
        # Three forms → C(3,2) = 3 unique pairs
        assert len(result) >= 1, "expected at least one collision entry"
        assert all(r["canonical"] == "LEB23001" for r in result)
        # All returned dicts must have the required shape
        for r in result:
            assert "a" in r and "b" in r and "canonical" in r and "score" in r

    def test_identical_raw_forms_not_a_collision(self):
        # Same raw string appearing twice — should NOT generate a collision because
        # it's the same raw form (raw forms go into a set)
        result = suggest_collisions(["LEB-23-001", "LEB-23-001"])
        assert result == [], (
            "identical raw strings do not constitute a canonical collision"
        )

    def test_empty_input_returns_empty(self):
        assert suggest_collisions([]) == []

    def test_collision_dict_shape(self):
        ids = ["LEB-23-001", "LEB23001"]
        result = suggest_collisions(ids)
        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) >= {"a", "b", "canonical", "score"}
        assert isinstance(entry["score"], (int, float))
        assert entry["canonical"] == "LEB23001"

    def test_non_colliding_different_ids_no_false_positive(self):
        ids = ["LEB23001", "LEB23002", "LEB23003"]
        result = suggest_collisions(ids)
        # These all have unique canonicals so no collision
        assert result == []

    def test_none_and_blank_ids_are_ignored(self):
        # canonicalize returns None for blank/None — should not raise or produce
        # phantom collisions
        ids = ["LEB-23-001", "", "LEB23001"]
        result = suggest_collisions(ids)
        # blank string canonicalizes to None, so only LEB-23-001 vs LEB23001 collision
        assert len(result) >= 1
        assert all(r["canonical"] == "LEB23001" for r in result)
