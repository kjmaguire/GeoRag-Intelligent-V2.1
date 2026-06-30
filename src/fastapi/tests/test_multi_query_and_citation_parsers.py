"""Tests for the 3 new JSON-output parsers — 2026-06-02.

Covers the LLM-output parsers in:
  - app.services.multi_query_expansion._parse_llm_json
  - app.services.atomic_claim_extractor._parse_extractor_json
  - app.services.sentence_grounding._parse_verifier_json

Each parser must handle:
  - clean JSON
  - markdown-fenced JSON
  - JSON with surrounding prose
  - empty / malformed input
"""

from __future__ import annotations

import pytest

from app.services.atomic_claim_extractor import _parse_extractor_json
from app.services.multi_query_expansion import _parse_llm_json
from app.services.sentence_grounding import _parse_verifier_json


class TestMultiQueryParser:
    def test_clean_json(self):
        raw = '{"expansions": ["a", "b", "c"]}'
        assert _parse_llm_json(raw, 3) == ["a", "b", "c"]

    def test_truncates_to_expected_n(self):
        raw = '{"expansions": ["a", "b", "c", "d", "e"]}'
        assert _parse_llm_json(raw, 3) == ["a", "b", "c"]

    def test_strips_markdown_fences(self):
        raw = '```json\n{"expansions": ["x", "y"]}\n```'
        assert _parse_llm_json(raw, 3) == ["x", "y"]

    def test_handles_surrounding_prose(self):
        raw = 'Here are my answers:\n{"expansions": ["one", "two"]}\nDone.'
        assert _parse_llm_json(raw, 3) == ["one", "two"]

    def test_filters_empty_strings(self):
        raw = '{"expansions": ["", "real", "   "]}'
        assert _parse_llm_json(raw, 3) == ["real"]

    def test_filters_overlong(self):
        long_str = "x" * 500
        raw = f'{{"expansions": ["{long_str}", "short"]}}'
        # The 500-char one exceeds the 400-char cap, the short one stays.
        assert _parse_llm_json(raw, 3) == ["short"]

    def test_raises_on_missing_object(self):
        with pytest.raises(ValueError):
            _parse_llm_json("nothing useful here", 3)

    def test_raises_on_non_list(self):
        with pytest.raises(ValueError):
            _parse_llm_json('{"expansions": "not a list"}', 3)


class TestAtomicClaimParser:
    def test_clean_json(self):
        raw = (
            '{"claims": [{"text": "PLS-22-08 has total depth 510m", '
            '"subject": "PLS-22-08", "predicate": "has total depth", '
            '"value": "510m"}]}'
        )
        out = _parse_extractor_json(raw, "chunk-abc")
        assert len(out) == 1
        assert out[0].text == "PLS-22-08 has total depth 510m"
        assert out[0].source_chunk_id == "chunk-abc"
        assert out[0].subject == "PLS-22-08"

    def test_caps_at_8_claims(self):
        claims = [{"text": f"claim {i}"} for i in range(20)]
        raw = f'{{"claims": {claims}}}'.replace("'", '"')
        out = _parse_extractor_json(raw, "chunk-1")
        assert len(out) == 8

    def test_empty_claim_list_is_ok(self):
        raw = '{"claims": []}'
        assert _parse_extractor_json(raw, "chunk-x") == []

    def test_drops_empty_text(self):
        raw = '{"claims": [{"text": ""}, {"text": "real claim"}]}'
        out = _parse_extractor_json(raw, "c1")
        assert len(out) == 1
        assert out[0].text == "real claim"

    def test_drops_overlong_text(self):
        long = "x" * 500
        raw = f'{{"claims": [{{"text": "{long}"}}, {{"text": "short"}}]}}'
        out = _parse_extractor_json(raw, "c1")
        assert len(out) == 1


class TestVerifierParser:
    def test_supported_true(self):
        raw = '{"supported": true, "rationale": "claim directly stated in chunk 1"}'
        supported, rationale = _parse_verifier_json(raw)
        assert supported is True
        assert "directly" in rationale

    def test_supported_false(self):
        raw = '{"supported": false, "rationale": "claim introduces a value not in any chunk"}'
        supported, _ = _parse_verifier_json(raw)
        assert supported is False

    def test_markdown_fenced(self):
        raw = '```json\n{"supported": true, "rationale": "ok"}\n```'
        supported, _ = _parse_verifier_json(raw)
        assert supported is True

    def test_missing_supported_defaults_false(self):
        raw = '{"rationale": "no verdict"}'
        supported, _ = _parse_verifier_json(raw)
        assert supported is False

    def test_truncates_long_rationale(self):
        long_rationale = "x" * 500
        raw = f'{{"supported": true, "rationale": "{long_rationale}"}}'
        _, rationale = _parse_verifier_json(raw)
        assert len(rationale) <= 300

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError):
            _parse_verifier_json("not json at all")
