"""Unit tests for silver_structure_populate — ADR-0007 PR-2.

Tests exercise the pure-function extractor (no DB, no Dagster, no MinIO)
across the notation patterns called out in the asset docstring.

Run with:
    pytest src/dagster/tests/test_silver_structure_populate.py -v
"""

from __future__ import annotations

import pytest

from georag_dagster.assets.silver_structure_populate import (
    VALID_STRUCTURE_TYPES,
    _classify_structure_type,
    _dedupe_candidates,
    _flatten_sections_text,
    _resolve_dip_direction,
    extract_structure_candidates,
)


# ---------------------------------------------------------------------------
# classify_structure_type
# ---------------------------------------------------------------------------

class TestClassifyStructureType:
    def test_kind_hint_canonical(self):
        assert _classify_structure_type("", kind_hint="foliation") == "foliation"
        assert _classify_structure_type("", kind_hint="Joint") == "joint"

    def test_s1_s2_s3_map_to_foliation(self):
        assert _classify_structure_type("", kind_hint="S1") == "foliation"
        assert _classify_structure_type("", kind_hint="s2") == "foliation"

    def test_window_keyword_fallback(self):
        assert _classify_structure_type("massive shear zone with infill") == "shear"
        assert _classify_structure_type("hairline joint set") == "joint"

    def test_no_keyword_falls_to_other(self):
        assert _classify_structure_type("just some random text") == "other"


# ---------------------------------------------------------------------------
# resolve_dip_direction
# ---------------------------------------------------------------------------

class TestResolveDipDirection:
    def test_no_quadrant_returns_strike_plus_90(self):
        td, tdd = _resolve_dip_direction(45.0, 30.0, None)
        assert td == 30.0
        assert tdd == 135.0

    def test_quadrant_picks_closer_candidate(self):
        # strike 45 → candidates 135 (SE) and 315 (NW). "SE" → 135.
        td, tdd = _resolve_dip_direction(45.0, 72.0, "SE")
        assert td == 72.0
        assert tdd == 135.0
        # Same strike with "NW" → 315.
        td, tdd = _resolve_dip_direction(45.0, 72.0, "NW")
        assert tdd == 315.0

    def test_missing_strike_returns_none(self):
        assert _resolve_dip_direction(None, 30.0, "SE") == (None, None)


# ---------------------------------------------------------------------------
# extract_structure_candidates — pattern coverage
# ---------------------------------------------------------------------------

class TestExtractStrikeDipQuadrant:
    def test_kind_prefixed_pattern(self):
        text = "S1 foliation 045/72 SE — strong fabric"
        hits = extract_structure_candidates(text=text, collar_id="c1", depth=12.5)
        assert len(hits) == 1
        h = hits[0]
        assert h["structure_type"] == "foliation"
        assert h["true_dip"] == 72.0
        assert h["true_dip_dir"] == 135.0
        assert h["alpha_angle"] is None
        assert h["collar_id"] == "c1"
        assert h["depth"] == 12.5
        assert "045/72" in h["notes"]

    def test_bare_strike_dip_requires_keyword(self):
        # Plain strike/dip number with NO structural keyword nearby → skipped.
        text = "core run 045/72 m recovered"
        hits = extract_structure_candidates(text=text, collar_id="c1", depth=None)
        assert hits == []

    def test_joint_set_pattern(self):
        text = "joint set 080/55 NE in fresh granite"
        hits = extract_structure_candidates(text=text, collar_id="c1", depth=None)
        assert len(hits) == 1
        assert hits[0]["structure_type"] == "joint"
        assert hits[0]["true_dip"] == 55.0
        # strike 80, NE quadrant 45 → cand 170 vs 350; 170 is closer to 45?
        # circ_dist(170, 45)=125, circ_dist(350, 45)=55. → 350.
        assert hits[0]["true_dip_dir"] == 350.0


class TestExtractLabeledStrikeDip:
    def test_labeled_strike_dip(self):
        text = "Measured strike 215, dip 60 SW in the hanging wall fault zone."
        hits = extract_structure_candidates(text=text, collar_id="c2", depth=88.0)
        assert len(hits) == 1
        h = hits[0]
        assert h["structure_type"] == "fault"
        assert h["true_dip"] == 60.0
        # strike 215, SW(225) → cand 305 or 125. circ(305,225)=80; circ(125,225)=100 → 305
        assert h["true_dip_dir"] == 305.0

    def test_labeled_with_degree_symbols(self):
        text = "strike: 045°, dip: 72° NE on the foliation surface"
        hits = extract_structure_candidates(text=text, collar_id="c2", depth=None)
        assert len(hits) == 1
        assert hits[0]["structure_type"] == "foliation"
        assert hits[0]["true_dip"] == 72.0


class TestExtractAlphaBeta:
    def test_alpha_beta_with_symbols(self):
        text = "Acoustic televiewer α=43° β=128° in the broken joint zone."
        hits = extract_structure_candidates(text=text, collar_id="c3", depth=200.0)
        assert len(hits) == 1
        h = hits[0]
        assert h["alpha_angle"] == 43.0
        assert h["beta_angle"] == 128.0
        assert h["true_dip"] is None
        assert h["structure_type"] == "joint"

    def test_alpha_beta_word_form(self):
        text = "atv: alpha 43 beta 128 in the bedding contact"
        hits = extract_structure_candidates(text=text, collar_id="c3", depth=None)
        assert len(hits) == 1
        assert hits[0]["alpha_angle"] == 43.0
        assert hits[0]["beta_angle"] == 128.0

    def test_alpha_beta_rejects_out_of_range(self):
        # alpha > 90 is invalid
        text = "alpha 130 beta 50 — bad row"
        hits = extract_structure_candidates(text=text, collar_id="c3", depth=None)
        assert hits == []


class TestExtractKindPrefixedSlash:
    def test_foliation_t_p_form(self):
        text = "foliation: 080° / 35°"
        hits = extract_structure_candidates(text=text, collar_id="c4", depth=None)
        assert len(hits) == 1
        assert hits[0]["structure_type"] == "foliation"
        assert hits[0]["true_dip"] == 35.0


class TestExtractMultipleHits:
    def test_multiple_distinct_hits(self):
        text = (
            "joint set 045/72 SE, fault zone 215/30 W, "
            "and α=43° β=128° on the cleavage face."
        )
        hits = extract_structure_candidates(text=text, collar_id="c5", depth=10.0)
        types = sorted(h["structure_type"] for h in hits)
        assert "joint" in types
        assert "fault" in types
        # The α/β hit gets a cleavage classification from the window context
        assert any(h["alpha_angle"] is not None for h in hits)

    def test_empty_text_returns_empty(self):
        assert extract_structure_candidates(text="", collar_id="c", depth=0.0) == []
        assert extract_structure_candidates(text=None, collar_id="c", depth=0.0) == []


# ---------------------------------------------------------------------------
# dedupe + flatten helpers
# ---------------------------------------------------------------------------

class TestDedupe:
    def test_dedupe_against_existing_keys(self):
        existing = {("c1", 12.5, "foliation", None, None)}
        cands = [
            {"collar_id": "c1", "depth": 12.5, "structure_type": "foliation",
             "alpha_angle": None, "beta_angle": None},
            {"collar_id": "c1", "depth": 12.5, "structure_type": "joint",
             "alpha_angle": None, "beta_angle": None},
        ]
        out = _dedupe_candidates(cands, existing_keys=existing)
        assert len(out) == 1
        assert out[0]["structure_type"] == "joint"

    def test_dedupe_within_batch(self):
        cands = [
            {"collar_id": "c1", "depth": 5.0, "structure_type": "joint",
             "alpha_angle": None, "beta_angle": None},
            {"collar_id": "c1", "depth": 5.0, "structure_type": "joint",
             "alpha_angle": None, "beta_angle": None},
        ]
        out = _dedupe_candidates(cands, existing_keys=set())
        assert len(out) == 1


class TestFlattenSectionsText:
    def test_string_passes_through(self):
        assert _flatten_sections_text("hello") == "hello"

    def test_none_returns_empty(self):
        assert _flatten_sections_text(None) == ""

    def test_dict_concatenates_values(self):
        s = _flatten_sections_text({"a": "alpha 43 beta 128", "b": "joint 045/72"})
        assert "alpha 43" in s
        assert "joint 045/72" in s

    def test_list_of_dicts(self):
        s = _flatten_sections_text([
            {"title": "Section 1", "text": "strike 215, dip 60 SW"},
            {"title": "Section 2", "text": "α=43° β=128°"},
        ])
        assert "strike 215" in s
        assert "α=43" in s


# ---------------------------------------------------------------------------
# Validity of structure_type vocabulary
# ---------------------------------------------------------------------------

class TestStructureTypeVocab:
    @pytest.mark.parametrize("expected", VALID_STRUCTURE_TYPES)
    def test_every_canonical_value_is_lowercase_word(self, expected):
        assert expected == expected.lower()
        assert " " not in expected
