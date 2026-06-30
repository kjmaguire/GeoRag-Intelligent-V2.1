"""Unit tests for R9 bounded escalation via LLM query rephrasing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.escalation import _parse_rephrasings_json, rephrase_query

# ── JSON parser ──────────────────────────────────────────────────────────


class TestParseRephrasingsJson:
    def test_clean_json(self):
        text = '{"rephrasings": ["alt one", "alt two"]}'
        assert _parse_rephrasings_json(text, 3) == ["alt one", "alt two"]

    def test_code_fence_wrapped(self):
        text = '```json\n{"rephrasings": ["alt"]}\n```'
        assert _parse_rephrasings_json(text, 3) == ["alt"]

    def test_with_leading_chatter(self):
        text = 'Here you go:\n{"rephrasings": ["alt"]}\nCheers!'
        assert _parse_rephrasings_json(text, 3) == ["alt"]

    def test_caps_at_max_count(self):
        text = '{"rephrasings": ["one", "two", "three", "four", "five"]}'
        assert _parse_rephrasings_json(text, 2) == ["one", "two"]

    def test_returns_empty_on_bad_json(self):
        assert _parse_rephrasings_json('not json at all', 3) == []

    def test_returns_empty_when_key_missing(self):
        assert _parse_rephrasings_json('{"other": []}', 3) == []

    def test_returns_empty_when_value_not_a_list(self):
        assert _parse_rephrasings_json('{"rephrasings": "hi"}', 3) == []

    def test_strips_non_string_items(self):
        text = '{"rephrasings": ["ok", 42, null, "also ok"]}'
        assert _parse_rephrasings_json(text, 5) == ["ok", "also ok"]


# ── rephrase_query ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_empty_when_client_is_none():
    assert await rephrase_query("any", anthropic_client=None) == []


@pytest.mark.asyncio
async def test_returns_empty_when_disabled(monkeypatch):
    from app.agent import escalation as esc

    monkeypatch.setattr(esc.settings, "AGENTIC_ESCALATION_ENABLED", False, raising=False)

    # Even with a mock client, disabled flag short-circuits.
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    assert await rephrase_query("any", anthropic_client=client) == []


@pytest.mark.asyncio
async def test_returns_empty_on_timeout():
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        )
    )
    assert await rephrase_query("what", anthropic_client=client) == []


@pytest.mark.asyncio
async def test_returns_empty_on_unexpected_error():
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError("kaboom"))
        )
    )
    assert await rephrase_query("what", anthropic_client=client) == []


@pytest.mark.asyncio
async def test_parses_anthropic_response(monkeypatch):
    """End-to-end: mock Anthropic returns a content-block list, we extract."""
    from app.agent import escalation as esc

    monkeypatch.setattr(esc.settings, "AGENTIC_ESCALATION_ENABLED", True, raising=False)
    monkeypatch.setattr(esc.settings, "AGENTIC_ESCALATION_MAX_REPHRASINGS", 3, raising=False)

    fake_msg = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text='{"rephrasings": ["alt 1", "alt 2"]}'),
        ]
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=fake_msg))
    )

    result = await rephrase_query(
        "original query",
        attempted_tools=["search_documents"],
        anthropic_client=client,
    )
    assert result == ["alt 1", "alt 2"]


@pytest.mark.asyncio
async def test_drops_rephrasings_identical_to_original(monkeypatch):
    from app.agent import escalation as esc

    monkeypatch.setattr(esc.settings, "AGENTIC_ESCALATION_ENABLED", True, raising=False)

    fake_msg = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="text",
                text='{"rephrasings": ["what deposit does this project host?", "uranium deposit type"]}',
            )
        ]
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=fake_msg))
    )

    result = await rephrase_query(
        "What deposit does this project host?",
        anthropic_client=client,
    )
    # The identical-to-original alternative must be dropped; the novel one kept.
    assert result == ["uranium deposit type"]
