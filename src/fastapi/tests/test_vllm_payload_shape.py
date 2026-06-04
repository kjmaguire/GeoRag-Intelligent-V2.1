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


# Phase-2 cleanup landed in orchestrator (top-level top_p/top_k/min_p/presence_penalty + response_format) — unskipped 2026-06-03 per audit item I1.
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


# Phase-2 cleanup landed in orchestrator (top-level top_p/top_k/min_p/presence_penalty + response_format) — unskipped 2026-06-03 per audit item I1.
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


# Phase-2 cleanup landed in orchestrator (top-level top_p/top_k/min_p/presence_penalty + response_format) — unskipped 2026-06-03 per audit item I1.
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


# Phase-2 cleanup landed in orchestrator (top-level top_p/top_k/min_p/presence_penalty + response_format) — unskipped 2026-06-03 per audit item I1.
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
async def test_vllm_guided_json_forwarded_to_engine():
    """When the caller passes a `guided_json` schema, it lands at the top
    level of the request body so vLLM's xgrammar backend can constrain
    decoding. The schema MUST appear verbatim — re-encoding via
    `extra_body` (an OpenAI-SDK convention) would not work for raw HTTPX."""
    client, captured = _make_capturing_client()

    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "boolean"}},
        "required": ["verdict"],
        "additionalProperties": False,
    }

    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=False,
        response_format="json",
        guided_json=schema,
    )

    payload = captured["json"]
    assert payload.get("guided_json") == schema, (
        "guided_json was not forwarded verbatim to the vLLM request body — "
        "schema-constrained decoding will not engage."
    )
    # And `response_format` JSON-mode is still set so the structured
    # presence_penalty arithmetic applies.
    assert payload.get("response_format") == {"type": "json_object"}
    assert payload.get("presence_penalty") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_vllm_guided_json_absent_when_not_passed():
    """No silent injection — when guided_json is None the field must not
    appear on the wire. Defends against future refactors that bind a
    default schema and break callers that intentionally want any-JSON."""
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
    assert "guided_json" not in payload


@pytest.mark.asyncio
async def test_vllm_guided_json_works_without_response_format():
    """Schema-constrained decoding can be requested without ALSO asking
    for json_object mode — vLLM's xgrammar still enforces the schema, and
    the structured presence_penalty must NOT be applied in this case
    (the caller didn't opt into JSON-mode wire shaping)."""
    client, captured = _make_capturing_client()

    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}

    await _call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        http_client=client,
        enable_thinking=False,
        guided_json=schema,
    )

    payload = captured["json"]
    assert payload.get("guided_json") == schema
    assert "response_format" not in payload
    # Still on the free-text presence-penalty path (no JSON-mode opt-in).
    assert payload.get("presence_penalty") == pytest.approx(1.5)


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


# Phase-2 cleanup landed in orchestrator (top-level top_p/top_k/min_p/presence_penalty + response_format) — unskipped 2026-06-03 per audit item I1.
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


@pytest.mark.asyncio
async def test_vllm_adhoc_client_uses_split_timeout(monkeypatch):
    """When no pooled http_client is supplied, the ad-hoc fallback in
    `_call_openai_compatible_llm` must construct httpx.AsyncClient with a
    split timeout (5s connect, TIMEOUT_GATHER_S read). A single-float
    timeout regression would let vLLM-down conditions hang for the full
    read budget before failing over."""
    import httpx as _httpx
    from app.agent import llm_calls

    monkeypatch.setattr(llm_calls.settings, "TIMEOUT_GATHER_S", 8.0, raising=False)

    captured: dict[str, object] = {}
    real_async_client = _httpx.AsyncClient

    class _Probe(real_async_client):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            super().__init__(*args, **kwargs)

        async def post(self, *args, **kwargs):  # type: ignore[override]
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {
                    "choices": [{"message": {"content": "ok", "reasoning": ""}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

    monkeypatch.setattr(llm_calls.httpx, "AsyncClient", _Probe)

    await llm_calls._call_openai_compatible_llm(
        user_message="hi",
        temperature=0.0,
        base_url="http://vllm:8000/v1",
        model="Qwen/Qwen3-14B-AWQ",
        enable_thinking=False,
    )

    timeout = captured.get("timeout")
    assert isinstance(timeout, _httpx.Timeout), (
        "ad-hoc httpx.AsyncClient was constructed with a non-Timeout value — "
        "regressed back to single-float TIMEOUT_GATHER_S which hides vLLM-down."
    )
    assert timeout.connect == pytest.approx(5.0)
    assert timeout.read == pytest.approx(8.0)
    assert timeout.write == pytest.approx(5.0)
    assert timeout.pool == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_call_llm_forwards_guided_json_to_vllm_branch(monkeypatch):
    """The `_call_llm` dispatcher must forward guided_json through to
    `_call_openai_compatible_llm` when LLM_BACKEND=vllm. Regression guard:
    a kwarg dropped at the dispatcher layer would silently disable
    schema-constrained decoding for every structured caller."""
    from app.agent import llm_calls

    monkeypatch.setattr(llm_calls.settings, "LLM_BACKEND", "vllm", raising=False)
    monkeypatch.setattr(llm_calls.settings, "VLLM_URL", "http://vllm:8000/v1", raising=False)
    monkeypatch.setattr(llm_calls.settings, "VLLM_MODEL", "Qwen/Qwen3-14B-AWQ", raising=False)
    monkeypatch.setattr(llm_calls.settings, "MAX_LLM_CALLS_PER_QUERY", 8, raising=False)
    # Reset the per-run call counter so this test isn't affected by ordering.
    llm_calls._llm_call_counter.set(0)

    client, captured = _make_capturing_client()
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }

    await llm_calls._call_llm(
        query="what is the depth of PLS-22-08?",
        context="The hole PLS-22-08 reached 510 m.",
        temperature=0.0,
        openai_http_client=client,
        system_prompt="test-system",
        response_format="json",
        guided_json=schema,
        audit_label="test",
    )

    payload = captured["json"]
    assert payload.get("guided_json") == schema
