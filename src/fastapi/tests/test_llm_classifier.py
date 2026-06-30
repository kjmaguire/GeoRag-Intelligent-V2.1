"""Unit tests for the LLM-based classifier fallback tier."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.llm_classifier import _parse_classifier_json, classify_via_llm

# ── JSON parser ──────────────────────────────────────────────────────────


class TestParseClassifierJson:
    def test_clean_json(self):
        text = '{"spatial":true,"documents":false,"graph":true,"assay":false,"downhole":false,"targeting":false,"public_geoscience":false}'
        result = _parse_classifier_json(text)
        assert result["spatial"] is True
        assert result["graph"] is True
        assert result["documents"] is False

    def test_code_fence_wrapped(self):
        text = '```json\n{"spatial":true,"documents":true,"graph":false,"assay":false,"downhole":false,"targeting":false,"public_geoscience":false}\n```'
        result = _parse_classifier_json(text)
        assert result["spatial"] is True
        assert result["documents"] is True

    def test_missing_keys_default_to_false(self):
        text = '{"spatial":true}'
        result = _parse_classifier_json(text)
        assert result["spatial"] is True
        assert result["graph"] is False
        assert result["documents"] is False

    def test_non_boolean_values_default_to_false(self):
        text = '{"spatial":"yes","documents":1,"graph":null}'
        result = _parse_classifier_json(text)
        assert result["spatial"] is False
        assert result["documents"] is False
        assert result["graph"] is False

    def test_malformed_json_returns_all_false(self):
        result = _parse_classifier_json('not valid')
        assert all(v is False for v in result.values())


# ── classify_via_llm ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_none_when_disabled(monkeypatch):
    from app.agent import llm_classifier as lc

    monkeypatch.setattr(lc.settings, "LLM_CLASSIFIER_FALLBACK_ENABLED", False, raising=False)
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    result = await classify_via_llm("q", anthropic_client=client)
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_client_absent():
    result = await classify_via_llm("q", anthropic_client=None)
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_timeout(monkeypatch):
    from app.agent import llm_classifier as lc

    monkeypatch.setattr(lc.settings, "LLM_CLASSIFIER_FALLBACK_ENABLED", True, raising=False)
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        )
    )
    result = await classify_via_llm("q", anthropic_client=client)
    assert result is None


@pytest.mark.asyncio
async def test_parses_bucket_decision(monkeypatch):
    from app.agent import llm_classifier as lc

    monkeypatch.setattr(lc.settings, "LLM_CLASSIFIER_FALLBACK_ENABLED", True, raising=False)
    monkeypatch.setattr(lc.settings, "MODEL_TIER_FAST", "claude-haiku-4-5", raising=False)
    monkeypatch.setattr(lc.settings, "TIMEOUT_GATHER_S", 5.0, raising=False)

    # LLM response: "alteration assemblage" should route to graph + documents.
    fake_msg = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="text",
                text='{"spatial":false,"documents":true,"graph":true,"assay":false,"downhole":false,"targeting":false,"public_geoscience":false}',
            )
        ]
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=fake_msg))
    )
    result = await classify_via_llm(
        "what is the alteration assemblage at the deposit?",
        anthropic_client=client,
    )
    assert result is not None
    assert result["graph"] is True
    assert result["documents"] is True
    assert result["spatial"] is False


@pytest.mark.asyncio
async def test_all_false_when_llm_says_no_bucket(monkeypatch):
    """LLM can legitimately decide none of the buckets apply — we pass that
    through (caller decides whether to escalate further)."""
    from app.agent import llm_classifier as lc

    monkeypatch.setattr(lc.settings, "LLM_CLASSIFIER_FALLBACK_ENABLED", True, raising=False)

    fake_msg = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="text",
                text='{"spatial":false,"documents":false,"graph":false,"assay":false,"downhole":false,"targeting":false,"public_geoscience":false}',
            )
        ]
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=fake_msg))
    )
    result = await classify_via_llm("what is the capital of france?", anthropic_client=client)
    assert result is not None
    assert all(v is False for v in result.values())
