"""Chaos coverage for live classifier failure handling."""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.chaos


@pytest.mark.asyncio
async def test_llm_classifier_returns_none_on_error(monkeypatch):
    """Classifier LLM unavailability must not block retrieval."""
    from app.agent import llm_classifier as classifier

    monkeypatch.setattr(
        classifier.settings,
        "LLM_CLASSIFIER_FALLBACK_ENABLED",
        True,
        raising=False,
    )

    async def _boom(*args, **kwargs):
        raise ConnectionError("anthropic unreachable")

    client = SimpleNamespace(messages=SimpleNamespace(create=_boom))
    result = await classifier.classify_via_llm("q", anthropic_client=client)
    assert result is None
