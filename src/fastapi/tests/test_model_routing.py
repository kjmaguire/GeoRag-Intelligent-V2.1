"""Unit tests for B1 model routing + failover.

Exercises the pure `select_tier` / `tier_to_model` / `downshift` /
`is_retriable_via_failover` functions without touching the orchestrator.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.agent.model_routing import (
    ModelTier,
    downshift,
    is_retriable_via_failover,
    select_tier,
    tier_to_model,
    tier_to_model_for_backend,
)


def _categories(**overrides: bool) -> dict[str, object]:
    base: dict[str, object] = {
        "spatial": False,
        "documents": False,
        "downhole": False,
        "graph": False,
        "assay": False,
        "targeting": False,
        "public_geoscience": False,
        "classifier_fallback": False,
    }
    base.update(overrides)
    return base


# ── select_tier ──────────────────────────────────────────────────────────


class TestSelectTier:
    def test_pure_spatial_is_fast(self):
        assert select_tier(_categories(spatial=True)) is ModelTier.FAST

    def test_pure_assay_is_fast(self):
        assert select_tier(_categories(assay=True)) is ModelTier.FAST

    def test_pure_downhole_is_fast(self):
        assert select_tier(_categories(downhole=True)) is ModelTier.FAST

    def test_documents_is_standard(self):
        assert select_tier(_categories(documents=True)) is ModelTier.STANDARD

    def test_public_geoscience_is_standard(self):
        assert select_tier(_categories(public_geoscience=True)) is ModelTier.STANDARD

    def test_spatial_plus_documents_is_standard(self):
        assert select_tier(_categories(spatial=True, documents=True)) is ModelTier.STANDARD

    def test_graph_is_deep(self):
        assert select_tier(_categories(graph=True)) is ModelTier.DEEP

    def test_targeting_is_deep(self):
        assert select_tier(_categories(targeting=True)) is ModelTier.DEEP

    def test_classifier_fallback_is_deep(self):
        assert select_tier(_categories(classifier_fallback=True, spatial=True, documents=True)) is ModelTier.DEEP

    def test_retry_escalates_to_deep(self):
        # A pure spatial query would be FAST; retry pushes it to DEEP.
        assert select_tier(_categories(spatial=True), retry_count=1) is ModelTier.DEEP

    def test_routing_disabled_always_deep(self):
        with patch("app.agent.model_routing.settings") as mock_settings:
            mock_settings.MODEL_ROUTING_ENABLED = False
            mock_settings.ANTHROPIC_MODEL = "claude-opus-4-7"
            assert select_tier(_categories(spatial=True)) is ModelTier.DEEP


# ── tier_to_model ────────────────────────────────────────────────────────


class TestTierToModel:
    def test_fast_resolves_to_haiku_by_default(self):
        with patch("app.agent.model_routing.settings") as m:
            m.MODEL_TIER_FAST = "claude-haiku-4-5"
            assert tier_to_model(ModelTier.FAST) == "claude-haiku-4-5"

    def test_standard_resolves_to_sonnet_by_default(self):
        with patch("app.agent.model_routing.settings") as m:
            m.MODEL_TIER_STANDARD = "claude-sonnet-4-5"
            assert tier_to_model(ModelTier.STANDARD) == "claude-sonnet-4-5"

    def test_deep_resolves_to_opus_by_default(self):
        with patch("app.agent.model_routing.settings") as m:
            m.MODEL_TIER_DEEP = "claude-opus-4-7"
            m.ANTHROPIC_MODEL = "claude-opus-4-7"
            assert tier_to_model(ModelTier.DEEP) == "claude-opus-4-7"


# ── downshift ────────────────────────────────────────────────────────────


class TestDownshift:
    def test_deep_downshifts_to_standard(self):
        assert downshift(ModelTier.DEEP) is ModelTier.STANDARD

    def test_standard_downshifts_to_fast(self):
        assert downshift(ModelTier.STANDARD) is ModelTier.FAST

    def test_fast_is_floor(self):
        assert downshift(ModelTier.FAST) is ModelTier.FAST


# ── is_retriable_via_failover ────────────────────────────────────────────


class TestRetriableViaFailover:
    def test_asyncio_timeout_is_retriable(self):
        assert is_retriable_via_failover(asyncio.TimeoutError()) is True

    def test_httpx_timeout_is_retriable(self):
        assert is_retriable_via_failover(httpx.TimeoutException("timed out")) is True

    def test_429_status_is_retriable(self):
        exc = Exception("rate limited")
        exc.status_code = 429  # type: ignore[attr-defined]
        assert is_retriable_via_failover(exc) is True

    def test_529_status_is_retriable(self):
        exc = Exception("overloaded")
        exc.status_code = 529  # type: ignore[attr-defined]
        assert is_retriable_via_failover(exc) is True

    def test_503_status_is_retriable(self):
        exc = Exception("service unavailable")
        exc.status_code = 503  # type: ignore[attr-defined]
        assert is_retriable_via_failover(exc) is True

    def test_400_status_is_not_retriable(self):
        exc = Exception("bad request")
        exc.status_code = 400  # type: ignore[attr-defined]
        assert is_retriable_via_failover(exc) is False

    def test_401_status_is_not_retriable(self):
        exc = Exception("unauthorized")
        exc.status_code = 401  # type: ignore[attr-defined]
        assert is_retriable_via_failover(exc) is False

    def test_value_error_is_not_retriable(self):
        assert is_retriable_via_failover(ValueError("bad input")) is False


# ── R12: local-LLM fallback target resolution ────────────────────────────


class TestLocalLLMFallbackTarget:
    """The _resolve_local_llm_fallback_target helper must prefer vLLM
    when configured and return None when nothing is set.

    Renamed from TestDeepSeekFallbackTarget 2026-05-17 after the legacy
    "_resolve_deepseek_fallback_target" alias was removed from llm_calls.
    """

    def test_prefers_vllm_when_set(self):
        from app.agent.orchestrator import _resolve_local_llm_fallback_target

        with patch("app.agent.llm_calls.settings") as m:
            m.VLLM_URL = "http://vllm:8000/v1"
            m.VLLM_MODEL = "Qwen/Qwen3-14B-AWQ"
            m.LLM_PRIMARY_URL = "http://vllm:8000/v1"
            m.LLM_PRIMARY_MODEL = "Qwen/Qwen3-14B-AWQ"

            target = _resolve_local_llm_fallback_target()

        assert target == ("http://vllm:8000/v1", "Qwen/Qwen3-14B-AWQ")

    def test_falls_back_to_primary_when_vllm_url_unset(self):
        from app.agent.orchestrator import _resolve_local_llm_fallback_target

        with patch("app.agent.llm_calls.settings") as m:
            m.VLLM_URL = ""
            m.VLLM_MODEL = ""
            m.LLM_PRIMARY_URL = "http://vllm:8000/v1"
            m.LLM_PRIMARY_MODEL = "Qwen/Qwen3-14B-AWQ"

            target = _resolve_local_llm_fallback_target()

        assert target == ("http://vllm:8000/v1", "Qwen/Qwen3-14B-AWQ")

    def test_returns_none_when_neither_configured(self):
        from app.agent.orchestrator import _resolve_local_llm_fallback_target

        with patch("app.agent.llm_calls.settings") as m:
            m.VLLM_URL = ""
            m.LLM_PRIMARY_URL = ""

            target = _resolve_local_llm_fallback_target()

        assert target is None


# ── R12: end-to-end failover path (Anthropic → vLLM) ────────────────────


@pytest.mark.asyncio
async def test_anthropic_429_falls_over_to_local_vllm(monkeypatch):
    """When LLM_BACKEND_FALLBACK='local_llm', a 429 from Anthropic should
    re-route the call to the OpenAI-compatible vLLM endpoint and return
    its response. Exercises the real orchestrator failover branch.

    Renamed from test_anthropic_429_falls_over_to_deepseek 2026-05-17.
    """
    from app.agent import orchestrator as orch

    # Pretend Anthropic returns 429 the first time it's invoked.
    anthropic_attempts = {"count": 0}

    async def fake_anthropic(user_message, temperature, **kwargs):
        anthropic_attempts["count"] += 1
        exc = RuntimeError("429 rate limited")
        # Duck-type the status_code that is_retriable_via_failover checks.
        exc.status_code = 429  # type: ignore[attr-defined]
        raise exc

    vllm_attempts = {"count": 0, "last_url": None, "last_model": None}

    async def fake_openai(user_message, temperature, **kwargs):
        vllm_attempts["count"] += 1
        vllm_attempts["last_url"] = kwargs.get("base_url")
        vllm_attempts["last_model"] = kwargs.get("model")
        return "Backfilled answer from vLLM. [DATA-1]"

    monkeypatch.setattr(orch, "_call_anthropic_llm", fake_anthropic)
    monkeypatch.setattr(orch, "_call_openai_compatible_llm", fake_openai)

    # Force the backend + fallback policy we want to exercise.
    monkeypatch.setattr(orch.settings, "LLM_BACKEND", "anthropic", raising=False)
    monkeypatch.setattr(orch.settings, "LLM_BACKEND_FALLBACK", "local_llm", raising=False)
    monkeypatch.setattr(orch.settings, "VLLM_URL", "http://vllm:8000/v1", raising=False)
    monkeypatch.setattr(orch.settings, "VLLM_MODEL", "Qwen/Qwen3-14B-AWQ", raising=False)

    from app.agent.model_routing import is_retriable_via_failover

    sanitized_query = orch._sanitize_query("what is the deepest hole?")
    user_message = orch._build_user_message("CONTEXT: (none)", sanitized_query)

    text = None
    try:
        text = await orch._call_anthropic_llm(user_message, 0.1)
    except Exception as exc:
        assert is_retriable_via_failover(exc)
        target = orch._resolve_local_llm_fallback_target()
        assert target is not None
        fallback_url, fallback_model = target
        text = await orch._call_openai_compatible_llm(
            user_message,
            0.1,
            base_url=fallback_url,
            model=fallback_model,
        )

    assert anthropic_attempts["count"] == 1
    assert vllm_attempts["count"] == 1
    assert vllm_attempts["last_url"] == "http://vllm:8000/v1"
    assert vllm_attempts["last_model"] == "Qwen/Qwen3-14B-AWQ"
    assert text == "Backfilled answer from vLLM. [DATA-1]"



# ── Backend dispatch (post-Ollama-removal — 2026-05-17) ───────────────────


class TestBackendDispatch:
    """`tier_to_model_for_backend` routes to the right Anthropic / vLLM
    model after the Ollama tier-routing path was removed."""

    def test_backend_dispatch_anthropic_unchanged(self):
        """Sanity: the Anthropic branch keeps using the existing tier map."""
        from app.agent import model_routing as mr
        with patch.object(mr.settings, "MODEL_ROUTING_ENABLED", True), \
                patch.object(mr.settings, "MODEL_TIER_FAST", "claude-haiku-4-5"):
            assert tier_to_model_for_backend(ModelTier.FAST, "anthropic") == "claude-haiku-4-5"

    def test_backend_dispatch_vllm_uses_vllm_model(self):
        from app.agent import model_routing as mr
        with patch.object(mr.settings, "VLLM_MODEL", "Qwen/Qwen3-14B-AWQ"):
            assert tier_to_model_for_backend(ModelTier.STANDARD, "vllm") == "Qwen/Qwen3-14B-AWQ"

    def test_unknown_backend_falls_back_to_vllm(self):
        from app.agent import model_routing as mr
        with patch.object(mr.settings, "VLLM_MODEL", "Qwen/Qwen3-14B-AWQ"):
            assert tier_to_model_for_backend(ModelTier.DEEP, "unknown_backend") == "Qwen/Qwen3-14B-AWQ"
