"""CC-02 Item 1 — fuzzy rock_name → rock_code resolution.

Pins the contract for the resolve_rock_code helper that
bronze_to_silver/lithology.py uses to collapse 1,000+ historic vendor
codes into the ~30-entry seeded silver.rock_codes catalogue.

Behaviour matrix tested:
  - Exact preferred-system match wins with confidence 1.0
  - Exact fallback-system match wins with confidence 1.0
  - Fuzzy match above threshold wins with confidence = score / 100
  - Fuzzy match below threshold returns (None, None) — the row should
    still be persisted by the asset with rock_code NULL so the catalogue
    gap stays visible
  - Empty / None rock_name returns (None, None)
  - Preferred-system tie wins over fallback when fuzzy scores are equal
"""
from __future__ import annotations

import pytest

from georag_dagster.assets.bronze_to_silver.lithology import resolve_rock_code


@pytest.fixture
def code_map() -> dict[str, dict[str, str]]:
    """Mirror the rock_codes seed in 2026_05_20_060900 (subset)."""
    return {
        "NRCAN": {
            "granite": "GR",
            "gneiss": "GN",
            "quartzite": "QTZ",
            "sandstone": "MAS",
            "schist": "SCH",
            "pegmatite": "PEG",
            "mafic": "MAF",
        },
        "GSC": {
            "granite": "gran",
            "gneiss": "gnss",
            "quartzite": "qtzt",
            "sandstone": "sast",
            "schist": "schi",
            "pegmatite": "pegm",
            "gabbro": "gabb",
        },
    }


class TestExactMatch:
    def test_preferred_exact_match_returns_confidence_1(self, code_map):
        code, conf = resolve_rock_code("Granite", code_map, "NRCAN")
        assert code == "GR"
        assert conf == 1.0

    def test_fallback_exact_match_returns_confidence_1(self, code_map):
        # 'gabbro' only exists in GSC; NRCAN preferred should still find it.
        code, conf = resolve_rock_code("Gabbro", code_map, "NRCAN")
        assert code == "gabb"
        assert conf == 1.0

    def test_exact_match_is_case_insensitive(self, code_map):
        code, conf = resolve_rock_code("GRANITE", code_map, "NRCAN")
        assert code == "GR"
        assert conf == 1.0

    def test_exact_match_strips_whitespace(self, code_map):
        code, conf = resolve_rock_code("  granite  ", code_map, "NRCAN")
        assert code == "GR"
        assert conf == 1.0


class TestFuzzyMatch:
    def test_simple_typo_fuzzy_matches_above_threshold(self, code_map):
        # 'granitic' shares 7/7 trigrams with 'granite'; rapidfuzz
        # token_set_ratio comfortably above the default 60 floor.
        code, conf = resolve_rock_code("granitic", code_map, "NRCAN")
        assert code == "GR"
        assert conf is not None
        assert 0.6 <= conf < 1.0

    def test_compound_word_fuzzy_matches(self, code_map):
        # "weathered granite" should fuzzy-match granite via token_set_ratio.
        code, conf = resolve_rock_code("weathered granite", code_map, "NRCAN")
        assert code == "GR"
        assert conf is not None
        assert conf < 1.0

    def test_unrelated_rock_below_threshold_returns_none(self, code_map):
        # 'limestone' isn't in the test catalogue and shouldn't fuzzy-match
        # anything in it above the threshold.
        code, conf = resolve_rock_code("limestone", code_map, "NRCAN")
        assert code is None
        assert conf is None

    def test_raised_threshold_rejects_weaker_match(self, code_map):
        # 'granitic' resolves at the default 60 floor; raising the floor
        # to 95 forces the rejection path.
        code, conf = resolve_rock_code(
            "granitic", code_map, "NRCAN", fuzzy_threshold=95,
        )
        assert code is None
        assert conf is None


class TestEdgeCases:
    def test_none_rock_name_returns_none(self, code_map):
        code, conf = resolve_rock_code(None, code_map, "NRCAN")
        assert code is None
        assert conf is None

    def test_empty_rock_name_returns_none(self, code_map):
        code, conf = resolve_rock_code("", code_map, "NRCAN")
        assert code is None
        assert conf is None

    def test_whitespace_only_rock_name_returns_none(self, code_map):
        code, conf = resolve_rock_code("   ", code_map, "NRCAN")
        assert code is None
        assert conf is None

    def test_empty_code_map_returns_none(self):
        code, conf = resolve_rock_code("granite", {}, "NRCAN")
        assert code is None
        assert conf is None

    def test_preferred_system_absent_falls_back_to_other(self, code_map):
        # If the preferred system has zero entries, fallback should still
        # be searched.
        partial_map = {"NRCAN": {}, "GSC": code_map["GSC"]}
        code, conf = resolve_rock_code("granite", partial_map, "NRCAN")
        assert code == "gran"
        assert conf == 1.0


class TestPreferenceOnTie:
    def test_exact_match_in_preferred_beats_exact_in_fallback(self, code_map):
        # 'granite' exists in both NRCAN and GSC; preferred (NRCAN) wins.
        code, _ = resolve_rock_code("granite", code_map, "NRCAN")
        assert code == "GR"

        # Switching the preferred system flips which code is returned.
        code, _ = resolve_rock_code("granite", code_map, "GSC")
        assert code == "gran"
