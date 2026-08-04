"""Pins the Azure AI Foundry (Cohere Command A+) wire-format handling in
_call_openai_compatible_llm — empirically verified 2026-07-30 against a live
deployment (see CLAUDE.md and llm_calls.py's own docstring) but never
regression-tested. Companion to test_vllm_payload_shape.py /
test_qwen3_payload_shape.py, same fake-http_client convention (no live model,
no new test dependency — http_client is already an injectable seam per the
P1 #13 comment in llm_calls.py).

Two behaviors, both azure-only:
  1. Cohere wraps JSON-mode output in <|START_TEXT|>...<|END_TEXT|> sentinel
     tokens — must be stripped so callers' json.loads() sees clean output.
  2. Cohere's "budget exhausted by thinking" failure surfaces reasoning on
     `reasoning_content` (vLLM/Qwen3 uses `reasoning` instead) — same
     fallback-message behavior must fire for either field name.

Also asserts the azure-only guard doesn't fire on the vllm backend, so a
future refactor can't accidentally make sentinel-stripping backend-agnostic
(harmless today since vLLM never emits these tokens, but the guard existing
at all is the point of testing it).
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
async def test_vllm_backend_does_not_strip_sentinel_like_text(monkeypatch):
    """The stripping guard checks backend_kind == "azure" specifically — a
    vLLM response that happens to contain sentinel-shaped text (e.g. echoed
    back from a user's own prompt) must pass through unmodified."""
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

    assert result == "<|START_TEXT|>literal vllm content<|END_TEXT|>"


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
