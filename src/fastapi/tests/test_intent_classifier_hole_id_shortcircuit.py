"""Hole-ID short-circuit in the 6-intent classifier (2026-05-25 fix).

When the user names a specific drill hole the right routing is always
``factual_lookup`` → query_collar_details. Without the short-circuit
phrasings like "this hole please tell me about it, 36-1085" score 0 across
every keyword bucket, fall through to the LLM fallback, and get
mis-classed as coverage_gap (observed in production).
"""

from __future__ import annotations

import pytest

from app.agent.agentic_retrieval import classify_intent, classify_intent_sync


class _FakeAsyncClient:
    """Stand-in for the vLLM HTTP client — the LLM must NEVER be invoked
    when the short-circuit fires."""


# ---------------------------------------------------------------------------
# Short-circuit fires regardless of phrasing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "this hole please tell me about it, 36-1085",
        "tell me about hole 36-1085",
        "what is the depth of hole 36-1085",
        "show me drillhole PLS-22-08",
        "borehole DDH-2547 details please",
    ],
)
def test_short_circuit_returns_factual_lookup(query: str) -> None:
    got = classify_intent_sync(query)
    assert got.intent == "factual_lookup", (
        f"hole-id short-circuit failed for {query!r}: intent={got.intent}"
    )
    assert got.confidence == pytest.approx(1.0)
    assert "hole_id_detected" in got.matched_triggers
    assert got.used_llm_fallback is False


# ---------------------------------------------------------------------------
# Short-circuit fires BEFORE the LLM fallback
# ---------------------------------------------------------------------------


async def test_short_circuit_skips_llm_fallback(monkeypatch) -> None:
    """A hole-id query must NOT consult the LLM fallback even when a
    client is supplied. The earlier failing path (Kyle 2026-05-25)
    fell through to the LLM and got mis-classed as coverage_gap."""
    called: list[str] = []

    async def fake_call_llm(*args, **kwargs):  # pragma: no cover — must not run
        called.append("yes")
        return "coverage_gap"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    got = await classify_intent(
        "this hole please tell me about it, 36-1085",
        openai_http_client=_FakeAsyncClient(),
    )
    assert got.intent == "factual_lookup"
    assert got.used_llm_fallback is False
    assert called == []


# ---------------------------------------------------------------------------
# Non-hole queries still go through the normal keyword path
# ---------------------------------------------------------------------------


def test_non_hole_query_unchanged() -> None:
    got = classify_intent_sync(
        "Give me a breakdown of data collection techniques by year."
    )
    assert got.intent == "project_summary"


def test_bare_digit_pair_no_context_is_not_hole_id() -> None:
    """Bare digit ranges that match the numeric pattern but have no
    hole context word should NOT trigger the short-circuit."""
    got = classify_intent_sync("show me intervals from 20-30 metres")
    assert got.intent != "factual_lookup" or got.matched_triggers != ("hole_id_detected",)
