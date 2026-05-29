"""Payload-shape invariants for the vLLM call path.

These tests don't validate generation quality — they pin the wire-format
contract between the orchestrator and vLLM v0.21+ serving Qwen3-14B-AWQ.
The Ollama-native shape (`options.*` block, `think` key, `num_predict`)
was replaced when the vLLM cutover landed in 2026-05; the invariants
below now reflect what the OpenAI-compatible vLLM endpoint expects:

  - sampling knobs (`top_p`, `top_k`, `min_p`, `presence_penalty`) live
    at the top level — vLLM extends the OpenAI-compat API with them
    directly rather than nesting under `options`;
  - thinking-mode is controlled by `chat_template_kwargs.enable_thinking`,
    which vLLM forwards to the Qwen3 chat template's
    `apply_chat_template()`. Sending `False` suppresses `<think>` blocks
    at the model boundary (see app/agent/llm_calls.py:400-414);
  - `presence_penalty=1.5` only when thinking is OFF (Qwen team's
    repetition-loop mitigation — the reasoning phase breaks repetition
    naturally on thinking-ON paths);
  - `LLM_MAX_THINKING_TOKENS` bumps `max_tokens` only on thinking-ON
    calls so the reasoning trace doesn't eat into the visible-answer
    budget.

The tests inject a fake `http_client` and capture the kwargs passed to
`client.post(...)`. No live model required.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock  # noqa: F401  (kept for future fixture work)

import pytest

from app.agent.orchestrator import _call_openai_compatible_llm


def _make_capturing_client() -> tuple[Any, dict[str, Any]]:
    """Return an (async_client_stub, captured_kwargs) pair.

    The stub satisfies the duck-typing that `_call_openai_compatible_llm`
    relies on for the blocking path: an async `post(url, *, json=...)`
    that returns an object with `.raise_for_status()` and `.json()`.
    """

    captured: dict[str, Any] = {}

    fake_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {
            "choices": [
                {"message": {"content": "ok", "reasoning": ""}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        },
    )

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> Any:
        captured["url"] = url
        captured["json"] = json
        return fake_response

    client = SimpleNamespace(post=_post)
    return client, captured


@pytest.mark.asyncio
async def test_thinking_off_sends_chat_template_kwargs(monkeypatch):
    """When thinking is OFF, `chat_template_kwargs.enable_thinking=False`
    MUST appear in the payload — vLLM v0.21 forwards it to Qwen3's
    `apply_chat_template()` to suppress `<think>` blocks at the model
    boundary. Without it, every answer trips the §04i guards on the
    inline reasoning leakage.
    """
    client, captured = _make_capturing_client()

    await _call_openai_compatible_llm(
        user_message="hello",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=False,
    )

    payload = captured["json"]
    assert "chat_template_kwargs" in payload, (
        "chat_template_kwargs missing — vLLM needs enable_thinking=False "
        "to suppress Qwen3 <think> blocks on the structured-answer path"
    )
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    # The old Ollama-native `think` key must NOT bleed back in.
    assert "think" not in payload


@pytest.mark.asyncio
async def test_thinking_on_omits_chat_template_kwargs(monkeypatch):
    """When thinking is ON, `chat_template_kwargs` MUST be absent so the
    Qwen3 chat template falls through to its default (thinking enabled).
    A stray `enable_thinking=False` here would silently force-off the
    reasoning phase even though the caller asked for thinking-ON.
    """
    client, captured = _make_capturing_client()

    await _call_openai_compatible_llm(
        user_message="hello",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=True,
    )

    payload = captured["json"]
    assert "chat_template_kwargs" not in payload, (
        "chat_template_kwargs leaked onto a thinking-ON call — the template "
        "must fall through to its default when thinking is requested"
    )


@pytest.mark.asyncio
async def test_presence_penalty_only_when_thinking_off(monkeypatch):
    """Qwen team's published guidance: presence_penalty=1.5 ONLY when
    thinking is off (mitigates repetition loops on long structured
    outputs). On thinking-on calls the reasoning phase already breaks
    repetition naturally, so the penalty would just degrade quality.
    Lives at the top level on the vLLM path (OpenAI-compat extension)."""
    # thinking OFF → presence_penalty present at top level
    client_off, captured_off = _make_capturing_client()
    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client_off,
        enable_thinking=False,
    )
    payload_off = captured_off["json"]
    assert "presence_penalty" in payload_off
    assert payload_off["presence_penalty"] == pytest.approx(1.5)

    # thinking ON → presence_penalty absent
    client_on, captured_on = _make_capturing_client()
    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client_on,
        enable_thinking=True,
    )
    payload_on = captured_on["json"]
    assert "presence_penalty" not in payload_on, (
        "presence_penalty leaked onto a thinking-ON call — the Qwen team's "
        "guidance applies the penalty only on thinking-OFF paths"
    )


@pytest.mark.asyncio
async def test_qwen3_sampling_defaults_present(monkeypatch):
    """`top_p / top_k / min_p` from QWEN3_* settings flow into the
    top-level payload on every call. Lose any of these and vLLM falls
    back to its built-in defaults, which are sub-optimal for Qwen3."""
    client, captured = _make_capturing_client()

    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.7,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=False,
    )

    payload = captured["json"]
    assert payload["top_p"] == pytest.approx(0.8)
    assert payload["top_k"] == 20
    assert payload["min_p"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_thinking_token_bump_only_when_thinking_on(monkeypatch):
    """LLM_MAX_THINKING_TOKENS bumps max_tokens on thinking-ON calls so
    the reasoning trace doesn't eat into the visible-answer budget. On
    thinking-OFF calls the bump must NOT apply — every token of the
    budget is for the answer and a larger max_tokens only widens the
    repetition-loop blast radius."""
    from app.agent import orchestrator as orch

    monkeypatch.setattr(orch.settings, "LLM_MAX_OUTPUT_TOKENS", 4096, raising=False)
    monkeypatch.setattr(orch.settings, "LLM_MAX_THINKING_TOKENS", 2048, raising=False)
    # Give the dynamic-prompt-fit cap enough headroom so it doesn't clamp
    # max_tokens below the value we're asserting on. The system prompt
    # alone is ~5k chars (~2.3k tokens at the 2.2 chars/token estimate);
    # 32k leaves ample room for max_tokens=6144.
    monkeypatch.setattr(orch.settings, "VLLM_MAX_MODEL_LEN", 32768, raising=False)

    # thinking OFF → max_tokens == base output cap, no bump
    client_off, captured_off = _make_capturing_client()
    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client_off,
        enable_thinking=False,
    )
    assert captured_off["json"]["max_tokens"] == 4096

    # thinking ON → max_tokens bumped by LLM_MAX_THINKING_TOKENS
    client_on, captured_on = _make_capturing_client()
    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client_on,
        enable_thinking=True,
    )
    assert captured_on["json"]["max_tokens"] == 4096 + 2048


@pytest.mark.asyncio
async def test_zero_thinking_budget_disables_bump(monkeypatch):
    """Setting LLM_MAX_THINKING_TOKENS=0 disables the bump (escape hatch
    for operators who'd rather have a single fixed budget regardless of
    thinking state)."""
    from app.agent import orchestrator as orch

    monkeypatch.setattr(orch.settings, "LLM_MAX_OUTPUT_TOKENS", 4096, raising=False)
    monkeypatch.setattr(orch.settings, "LLM_MAX_THINKING_TOKENS", 0, raising=False)
    monkeypatch.setattr(orch.settings, "VLLM_MAX_MODEL_LEN", 32768, raising=False)

    client, captured = _make_capturing_client()
    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=True,
    )
    assert captured["json"]["max_tokens"] == 4096
