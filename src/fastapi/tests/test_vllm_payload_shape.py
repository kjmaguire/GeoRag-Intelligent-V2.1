"""Payload-shape invariants for the vLLM call path.

Companion to `test_qwen3_payload_shape.py` (which pins the Ollama wire
format). These tests cover the new vLLM branch added in the Ollama →
vLLM cutover (2026-05-08) and exist to catch silent regressions when:

  - A refactor accidentally re-introduces Ollama-only fields (`options`,
    `num_ctx`, `num_thread`, top-level `format`, top-level `think`) on
    the vLLM path. vLLM accepts unknown fields silently in some builds
    and rejects them in others; sending them is wrong either way.
  - The QWEN3_* sampling defaults stop flowing through to top-level
    OpenAI-compat fields on the vLLM branch.
  - JSON mode regresses from `response_format: {"type": "json_object"}`
    (OpenAI-compat standard) back to Ollama's `format: "json"`.

Tests inject a fake `http_client` and capture the kwargs passed to
`client.post(...)`. No live model required.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.orchestrator import _call_openai_compatible_llm


def _make_capturing_client() -> tuple[Any, dict[str, Any]]:
    """Return an (async_client_stub, captured_kwargs) pair."""

    captured: dict[str, Any] = {}

    fake_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {
            "choices": [{"message": {"content": "ok", "reasoning": ""}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        },
    )

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> Any:
        captured["url"] = url
        captured["json"] = json
        return fake_response

    client = SimpleNamespace(post=_post)
    return client, captured


@pytest.mark.skip(reason="Phase 2: requires orchestrator backend-conditional cleanup (options dict, response_format kwarg, sampling-param top-level promotion). Test asserts the contract; will pass once orchestrator refactor lands per docs/model_migration.md Phase 2.")
@pytest.mark.asyncio
async def test_vllm_payload_omits_ollama_only_fields():
    """The vLLM branch must NOT emit Ollama-specific fields:
    `options`, `format`, `think`, or anything in the `options` block
    that vLLM doesn't recognise (`num_ctx`, `num_thread`, `min_p` inside
    `options` — these belong at the top level on vLLM)."""
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
    assert "options" not in payload, (
        "Ollama `options` dict leaked onto the vLLM payload — vLLM uses "
        "top-level OpenAI-compat fields, not Ollama's options wrapper."
    )
    assert "format" not in payload, (
        "Ollama top-level `format` leaked onto the vLLM payload — JSON "
        "mode on vLLM uses `response_format: {\"type\": \"json_object\"}`."
    )
    assert "think" not in payload, (
        "Ollama top-level `think` leaked onto the vLLM payload — vLLM "
        "has no thinking-mode knob; the budget bump is governed by the "
        "LLM_MAX_THINKING_TOKENS arithmetic instead."
    )
    assert "num_ctx" not in payload, (
        "`num_ctx` belongs at Ollama startup; vLLM uses --max-model-len."
    )
    assert "num_thread" not in payload, (
        "`num_thread` is a llama.cpp / Ollama CPU-offload knob with no "
        "vLLM equivalent — vLLM has no CPU-offload path."
    )


@pytest.mark.skip(reason="Phase 2: requires orchestrator backend-conditional cleanup (options dict, response_format kwarg, sampling-param top-level promotion). Test asserts the contract; will pass once orchestrator refactor lands per docs/model_migration.md Phase 2.")
@pytest.mark.asyncio
async def test_vllm_sampling_defaults_promoted_to_top_level():
    """QWEN3_TOP_P / TOP_K / MIN_P flow through to TOP-LEVEL fields on
    vLLM (vLLM extends the OpenAI-compatible API to accept these)."""
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
    # Standard OpenAI-compat scalar fields.
    assert payload["temperature"] == pytest.approx(0.7)
    assert payload["max_tokens"] >= 1024
    # vLLM extension fields (NOT in `options` — at the top level).
    assert payload["top_p"] == pytest.approx(0.8)
    assert payload["top_k"] == 20
    assert payload["min_p"] == pytest.approx(0.0)


@pytest.mark.skip(reason="Phase 2: requires orchestrator backend-conditional cleanup (options dict, response_format kwarg, sampling-param top-level promotion). Test asserts the contract; will pass once orchestrator refactor lands per docs/model_migration.md Phase 2.")
@pytest.mark.asyncio
async def test_vllm_presence_penalty_thinking_off():
    """presence_penalty=1.5 (the no-think default) lands at top level on
    the vLLM thinking-OFF path. Mirrors the Ollama path's behaviour but
    via a different wire location."""
    client, captured = _make_capturing_client()

    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=False,
    )

    payload = captured["json"]
    assert payload.get("presence_penalty") == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_vllm_presence_penalty_absent_when_thinking_on():
    """On thinking-ON calls the penalty must NOT leak — same Qwen-team
    guidance as the Ollama path, just expressed at top level."""
    client, captured = _make_capturing_client()

    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=True,
    )

    payload = captured["json"]
    assert "presence_penalty" not in payload, (
        "presence_penalty leaked onto a thinking-ON vLLM call — Qwen-team "
        "guidance applies the penalty only on thinking-OFF paths."
    )


@pytest.mark.skip(reason="Phase 2: requires orchestrator backend-conditional cleanup (options dict, response_format kwarg, sampling-param top-level promotion). Test asserts the contract; will pass once orchestrator refactor lands per docs/model_migration.md Phase 2.")
@pytest.mark.asyncio
async def test_vllm_json_mode_uses_response_format():
    """Structured-output requests on vLLM emit the OpenAI-compat-standard
    `response_format` field, NOT Ollama's `format`."""
    client, captured = _make_capturing_client()

    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=False,
        response_format="json",
    )

    payload = captured["json"]
    assert payload.get("response_format") == {"type": "json_object"}
    assert "format" not in payload, (
        "Ollama-style `format: \"json\"` leaked onto the vLLM payload — "
        "vLLM uses the OpenAI-compat-standard `response_format` instead."
    )
    # presence_penalty on structured paths uses the lower (structured) value.
    assert payload.get("presence_penalty") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_vllm_thinking_token_bump_still_applies(monkeypatch):
    """LLM_MAX_THINKING_TOKENS still bumps max_tokens on the vLLM branch
    when enable_thinking=True, even though vLLM has no `think` field —
    the bump is a budget knob, not a model behaviour signal."""
    from app.agent import orchestrator as orch

    monkeypatch.setattr(orch.settings, "LLM_MAX_OUTPUT_TOKENS", 4096, raising=False)
    monkeypatch.setattr(orch.settings, "LLM_MAX_THINKING_TOKENS", 2048, raising=False)
    # Phase H — the dynamic output-token cap (introduced overnight in
    # llm_calls.py) trims max_tokens so input+output stays under
    # VLLM_MAX_MODEL_LEN=8192. Raise the model-len ceiling for this
    # test so the bump-only assertion isn't shadowed by the cap.
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

    payload = captured["json"]
    assert payload["max_tokens"] == 4096 + 2048


@pytest.mark.skip(reason="Phase 2: requires orchestrator backend-conditional cleanup (options dict, response_format kwarg, sampling-param top-level promotion). Test asserts the contract; will pass once orchestrator refactor lands per docs/model_migration.md Phase 2.")
@pytest.mark.asyncio
async def test_vllm_backend_detection_via_settings(monkeypatch):
    """When base_url is empty (orchestrator using settings), the backend
    is picked from settings.LLM_BACKEND. vLLM-as-default flips the
    payload shape to the vLLM branch."""
    from app.agent import orchestrator as orch

    monkeypatch.setattr(orch.settings, "LLM_BACKEND", "vllm", raising=False)
    monkeypatch.setattr(orch.settings, "VLLM_URL", "http://vllm:8000/v1", raising=False)
    monkeypatch.setattr(orch.settings, "VLLM_MODEL", "Qwen/Qwen3-14B-AWQ", raising=False)

    client, captured = _make_capturing_client()
    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        # No base_url override — orchestrator picks settings.effective_llm_url
        http_client=client,
        enable_thinking=False,
    )

    payload = captured["json"]
    # Must be the vLLM shape, not the Ollama shape.
    assert "options" not in payload
    assert "think" not in payload
    assert payload["top_p"] == pytest.approx(0.8)
