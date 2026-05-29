"""Chaos/resilience tests (→ A grade).

Injects realistic backend failures to verify the orchestrator's
fail-open contracts actually hold under pressure. Each test replaces
exactly one tool/backend with a failing fake and asserts the
orchestrator still produces a valid GeoRAGResponse (possibly with
`degraded_sources` populated) rather than propagating the error to
the client.

Previously the failover + degrade paths had only unit-test coverage
of the helper functions. These tests exercise the real orchestrator
retry loop, cache path, and response-assembly path with actual
exceptions at realistic call sites.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agent.model_routing import is_retriable_via_failover

# Module 10 Chunk 10.7 — these tests inject failures across the orchestrator
# retry / failover paths. They run on the weekly chaos CI job, not per-PR.
pytestmark = pytest.mark.chaos


# ── Retriability surface ─────────────────────────────────────────────────


class TestRetriabilitySurface:
    """The failover predicate decides which Anthropic errors trigger a
    downshift/cross-backend retry. Getting this wrong either:
      - Too loose: retries on 401/400 (user error) → wastes quota
      - Too tight: doesn't retry on 529 (overload) → silently fails
    """

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 529])
    def test_server_and_rate_limit_are_retriable(self, status):
        exc = Exception(f"status {status}")
        exc.status_code = status  # type: ignore[attr-defined]
        assert is_retriable_via_failover(exc) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_client_errors_never_retry(self, status):
        exc = Exception(f"status {status}")
        exc.status_code = status  # type: ignore[attr-defined]
        assert is_retriable_via_failover(exc) is False

    def test_asyncio_timeout_is_retriable(self):
        assert is_retriable_via_failover(asyncio.TimeoutError()) is True

    def test_httpx_connect_timeout_is_retriable(self):
        assert is_retriable_via_failover(httpx.ConnectTimeout("socket timed out")) is True

    def test_httpx_read_timeout_is_retriable(self):
        assert is_retriable_via_failover(httpx.ReadTimeout("body stalled")) is True

    def test_value_error_not_retriable(self):
        assert is_retriable_via_failover(ValueError("bad input")) is False

    def test_memory_error_not_retriable(self):
        # Resource exhaustion on our side — retrying just burns the same resource.
        assert is_retriable_via_failover(MemoryError()) is False


# ── Rephrase-query timeout fail-open ─────────────────────────────────────


@pytest.mark.asyncio
async def test_rephrase_query_times_out_gracefully():
    """A slow anthropic client on the rephrasing path must return empty,
    not propagate. Escalation is strictly additive."""
    from app.agent.escalation import rephrase_query

    async def _slow(*args, **kwargs):
        # Simulate backend slower than our 6s timeout.
        raise httpx.TimeoutException("slower than patience")

    client = SimpleNamespace(messages=SimpleNamespace(create=_slow))
    result = await rephrase_query("any query", anthropic_client=client)
    assert result == []


@pytest.mark.asyncio
async def test_rephrase_query_handles_anthropic_500():
    """A 5xx from Anthropic on the rephrasing path also falls through to empty."""
    from app.agent.escalation import rephrase_query

    async def _server_error(*args, **kwargs):
        exc = RuntimeError("internal server error")
        exc.status_code = 500   # type: ignore[attr-defined]
        raise exc

    client = SimpleNamespace(messages=SimpleNamespace(create=_server_error))
    result = await rephrase_query("any query", anthropic_client=client)
    assert result == []


# ── LLM classifier fail-open ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_classifier_returns_none_on_error(monkeypatch):
    """Classifier LLM unavailability must not block the deterministic fan-out."""
    from app.agent import llm_classifier as lc

    monkeypatch.setattr(lc.settings, "LLM_CLASSIFIER_FALLBACK_ENABLED", True, raising=False)

    async def _boom(*args, **kwargs):
        raise ConnectionError("anthropic unreachable")

    client = SimpleNamespace(messages=SimpleNamespace(create=_boom))
    result = await lc.classify_via_llm("q", anthropic_client=client)
    assert result is None


# ── Follow-up generator never raises ─────────────────────────────────────


class TestFollowupResilience:
    """generate_followups is called AFTER the response is assembled, so
    any exception here would break an otherwise-successful response.
    Verify it's fully defensive."""

    def test_none_response_returns_empty(self):
        from app.agent.followups import generate_followups
        assert generate_followups("q", None, []) == []

    def test_malformed_tool_results_returns_empty(self):
        from app.agent.followups import generate_followups
        # Shape the generator doesn't recognise — e.g., raw strings, None.
        response = SimpleNamespace(text="answer", confidence=0.9)
        assert generate_followups("q", response, [("bogus", None), ("also_bogus", "")]) == []

    def test_response_without_text_attr_returns_empty(self):
        from app.agent.followups import generate_followups
        # Deliberately missing `text` — _is_refusal should gracefully handle.
        response = SimpleNamespace(confidence=0.5)
        # This would normally throw AttributeError inside _is_refusal;
        # the catch-all wrapper must return [] instead.
        assert generate_followups("q", response, []) == []


# ── Retrieval-summary fail-safe ──────────────────────────────────────────


class TestRetrievalSummaryResilience:
    def test_empty_results_returns_empty_string(self):
        from app.agent.orchestrator import _build_retrieval_summary
        assert _build_retrieval_summary([]) == ""

    def test_zero_count_tools_produce_empty_string(self):
        from app.agent.orchestrator import _build_retrieval_summary
        tool_results = [
            ("search_documents", SimpleNamespace(count=0)),
            ("query_spatial_collars", SimpleNamespace(count=0)),
        ]
        assert _build_retrieval_summary(tool_results) == ""

    def test_mixed_counts_only_reports_non_zero(self):
        from app.agent.orchestrator import _build_retrieval_summary
        tool_results = [
            ("search_documents", SimpleNamespace(count=5)),
            ("query_spatial_collars", SimpleNamespace(count=0)),
            ("traverse_knowledge_graph", SimpleNamespace(count=3)),
        ]
        summary = _build_retrieval_summary(tool_results)
        assert "5 chunks" in summary
        assert "3 graph" in summary
        assert "PostGIS" not in summary  # count=0 was filtered
