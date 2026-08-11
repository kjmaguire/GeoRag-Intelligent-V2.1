"""Pins the Azure AI Foundry (Cohere Command A+) wire-format handling in
_call_openai_compatible_llm — empirically verified 2026-07-30 against a live
deployment (see CLAUDE.md and llm_calls.py's own docstring) but never
regression-tested. Companion to test_vllm_payload_shape.py /
test_qwen3_payload_shape.py, same fake-http_client convention (no live model,
no new test dependency — http_client is already an injectable seam per the
P1 #13 comment in llm_calls.py).

Two behaviors:
  1. Cohere wraps JSON-mode output in <|START_TEXT|>...<|END_TEXT|> sentinel
     tokens — must be stripped so callers' json.loads() sees clean output.
     The strip gate was deliberately widened 2026-08-10 from
     backend_kind == "azure" to ANY response content containing the sentinel:
     the sentinel is a property of the Cohere model, not the transport, so an
     operator serving Cohere behind their own OpenAI-compatible endpoint
     (LLM_BACKEND=vllm) gets the same stripping.
  2. (azure-only) Cohere's "budget exhausted by thinking" failure surfaces
     reasoning on `reasoning_content` (vLLM/Qwen3 uses `reasoning` instead) —
     same fallback-message behavior must fire for either field name.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.orchestrator import _call_openai_compatible_llm
from app.config import settings


def _make_capturing_client(content: str, reasoning_field: str = "reasoning") -> Any:
    fake_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {
            "choices": [{"message": {"content": content, reasoning_field: ""}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        },
    )

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> Any:
        return fake_response

    return SimpleNamespace(post=_post)


@pytest.mark.asyncio
async def test_azure_backend_strips_cohere_sentinel_tokens(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")
    client = _make_capturing_client('<|START_TEXT|>{"answer": "42"}<|END_TEXT|>')

    result = await _call_openai_compatible_llm(
        user_message="hello",
        temperature=0.0,
        base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
        model="Cohere-command-a-plus-05-2026",
        http_client=client,
        enable_thinking=False,
    )

    assert "<|START_TEXT|>" not in result
    assert "<|END_TEXT|>" not in result
    assert result == '{"answer": "42"}'


@pytest.mark.asyncio
async def test_vllm_backend_strips_sentinel_tokens_too(monkeypatch):
    """2026-08-10: the strip gate is content-based, not backend-based — the
    sentinel is a property of the Cohere model, not the transport. A vLLM
    (OpenAI-compatible) backend response containing <|START_TEXT|> sentinel
    tokens must be stripped exactly like the azure path."""
    monkeypatch.setattr(settings, "LLM_BACKEND", "vllm")
    client = _make_capturing_client("<|START_TEXT|>literal vllm content<|END_TEXT|>")

    result = await _call_openai_compatible_llm(
        user_message="hello",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=False,
    )

    assert "<|START_TEXT|>" not in result
    assert "<|END_TEXT|>" not in result
    assert result == "literal vllm content"


@pytest.mark.asyncio
async def test_azure_backend_reasoning_content_triggers_budget_fallback(monkeypatch):
    """Cohere's thinking-budget-exhausted failure surfaces on
    `reasoning_content`, not `reasoning` (the vLLM/Qwen3 field name) — empty
    `content` plus a non-empty `reasoning_content` must produce the same
    user-visible fallback message as the vLLM/Qwen3 `reasoning` path."""
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> Any:
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "choices": [{"message": {
                    "content": "",
                    "reasoning_content": "internal deliberation that ate the whole budget",
                }}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            },
        )

    client = SimpleNamespace(post=_post)

    result = await _call_openai_compatible_llm(
        user_message="hello",
        temperature=0.0,
        base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
        model="Cohere-command-a-plus-05-2026",
        http_client=client,
        enable_thinking=False,
    )

    assert result != ""
    assert "token budget" in result.lower()
