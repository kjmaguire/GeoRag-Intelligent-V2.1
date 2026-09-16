"""`app/agent/llm_cohere.py` must send and read what ADR-0023 says it does.

WHAT THIS FILE PROVES, AND WHAT IT DOES NOT
    It proves the adapter's behaviour against a transport under test control:
    what it puts on the wire, what it does with each response shape it claims
    to tolerate, and what it does when it recognises nothing at all.

    It proves nothing about Cohere. Every field is ``[UNVERIFIED]`` until a
    live call confirms it. The tests are written so that a real response
    shape differing from the assumed one shows up as a failing assertion with
    the actual body in the message, rather than as a blank answer in
    production — which is the whole reason the adapter raises instead of
    returning ``""``.

THE ONE THAT MATTERS MOST
    ``test_the_system_prompt_is_a_message_not_a_top_level_field``. Bedrock
    Converse takes ``system`` as a top-level parameter; Cohere v2 takes it as
    a message. Getting that backwards does not raise anything anywhere — the
    model still answers fluently, just without the grounding rules applied,
    and CLAUDE.md hard rule 4's citation guards would then be enforcing
    against output produced from a prompt that never carried them.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agent import llm_cohere
from app.agent.llm_cohere import CohereResponseShapeError, call_cohere_llm
from app.config import settings

# ---------------------------------------------------------------------------
# A transport under test control
# ---------------------------------------------------------------------------


def _install(
    monkeypatch: pytest.MonkeyPatch,
    handler,
    *,
    api_key: str = "test-only-not-a-real-key",
    model: str = "command-a-plus-05-2026",
) -> list[dict[str, Any]]:
    """Route the adapter's requests into ``handler`` and capture each body.

    ``httpx.AsyncClient`` is swapped for a factory that injects a
    ``MockTransport``, rather than stubbing the adapter's own functions: the
    request assembly is precisely what is under test, so it has to run for
    real and be observed at the boundary.
    """
    sent: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return handler(request)

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(llm_cohere.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(settings, "LLM_BACKEND", "cohere")
    monkeypatch.setattr(settings, "COHERE_API_KEY", api_key)
    monkeypatch.setattr(settings, "COHERE_CHAT_MODEL", model)
    return sent


def _ok(text: str = "An answer.", **usage: Any):
    """A well-formed non-streaming v2 reply, in the documented shape."""
    body: dict[str, Any] = {"message": {"content": [{"type": "text", "text": text}]}}
    if usage:
        body["usage"] = {"tokens": usage}
    return lambda _request: httpx.Response(200, json=body)


def _sse(*frames: str):
    """An SSE stream body built from raw frame payloads."""
    return lambda _request: httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(f"data: {frame}\n\n" for frame in frames).encode(),
    )


async def _collect(deltas: list[str]):
    async def _cb(piece: str) -> None:
        deltas.append(piece)

    return _cb


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_system_prompt_is_a_message_not_a_top_level_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one divergence from Converse that fails silently when reversed."""
    sent = _install(monkeypatch, _ok())
    await call_cohere_llm("the question", 0.2, system_prompt="GROUNDING RULES")

    body = sent[0]
    assert "system" not in body, (
        "`system` as a top-level key is the Bedrock Converse shape. Cohere v2 "
        "would ignore it, and a prompt missing its grounding rules still "
        "returns fluent text."
    )
    assert body["messages"][0] == {"role": "system", "content": "GROUNDING RULES"}
    assert body["messages"][-1] == {"role": "user", "content": "the question"}


@pytest.mark.asyncio
async def test_the_three_system_parts_are_joined_into_one_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(monkeypatch, _ok())
    await call_cohere_llm("q", 0.2, system_prompt="RULES", project_preamble="PREAMBLE", project_facts="FACTS")
    systems = [m for m in sent[0]["messages"] if m["role"] == "system"]
    assert len(systems) == 1
    assert systems[0]["content"] == "RULES\n\nPREAMBLE\n\nFACTS"


@pytest.mark.asyncio
async def test_no_system_prompt_sends_no_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(monkeypatch, _ok())
    await call_cohere_llm("q", 0.2)
    assert [m["role"] for m in sent[0]["messages"]] == ["user"]


@pytest.mark.asyncio
async def test_json_mode_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md hard rule 4 rides on this.

    Every typed-output guard in orchestrator_validators.py assumes the model
    was actually asked for JSON. If this field is dropped the guards start
    rejecting prose the model was never told not to write.
    """
    sent = _install(monkeypatch, _ok('{"answer": "x"}'))
    await call_cohere_llm("q", 0.2, response_format="json_object")
    assert sent[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_no_response_format_key_when_json_is_not_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(monkeypatch, _ok())
    await call_cohere_llm("q", 0.2)
    assert "response_format" not in sent[0]


@pytest.mark.asyncio
async def test_the_model_comes_from_the_cohere_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(monkeypatch, _ok(), model="command-a-plus-05-2026")
    await call_cohere_llm("q", 0.2)
    assert sent[0]["model"] == "command-a-plus-05-2026"


@pytest.mark.asyncio
async def test_the_output_request_is_capped_against_the_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cap_output_tokens` must be live on this path, not just imported.

    Without it a long retrieval context plus a full-size output request pushes
    ``prompt_tokens + max_tokens`` past the window and the provider answers
    400 — so the run fails AFTER paying to build the prompt. The guard did not
    survive the first draft of the Bedrock port, which is why it is asserted
    rather than assumed.
    """
    sent = _install(monkeypatch, _ok())
    monkeypatch.setattr(settings, "COHERE_CHAT_MAX_MODEL_LEN", 4_000)
    monkeypatch.setattr(settings, "COHERE_CHAT_MAX_TOKENS", 4_096)

    await call_cohere_llm("x" * 6_000, 0.2)

    assert sent[0]["max_tokens"] < 4_096
    assert sent[0]["max_tokens"] >= 64, "never ask for zero — see cap_output_tokens"


@pytest.mark.asyncio
async def test_an_empty_api_key_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _ok(), api_key="   ")
    with pytest.raises(RuntimeError, match="COHERE_API_KEY"):
        await call_cohere_llm("q", 0.2)


@pytest.mark.asyncio
async def test_the_bearer_key_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"message": {"content": [{"text": "ok"}]}})

    _install(monkeypatch, _handler)
    await call_cohere_llm("q", 0.2)
    assert seen["authorization"] == "Bearer test-only-not-a-real-key"


# ---------------------------------------------------------------------------
# Response reading — the tolerant half
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typed_content_blocks_are_concatenated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json={"message": {"content": [{"text": "one "}, {"text": "two"}]}},
        ),
    )
    assert await call_cohere_llm("q", 0.2) == "one two"


@pytest.mark.asyncio
async def test_a_bare_string_content_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tolerance is deliberate: a reader that returns the text beats a strict
    one that is right about the schema and returns nothing."""
    _install(monkeypatch, lambda _r: httpx.Response(200, json={"message": {"content": "flat"}}))
    assert await call_cohere_llm("q", 0.2) == "flat"


@pytest.mark.asyncio
async def test_a_top_level_text_field_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda _r: httpx.Response(200, json={"text": "pre-v2 spelling"}))
    assert await call_cohere_llm("q", 0.2) == "pre-v2 spelling"


@pytest.mark.asyncio
async def test_cohere_sentinel_tokens_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A property of the MODEL, so it applies on this host too.

    Confirmed on Azure AI Foundry 2026-07-30: JSON-mode output arrived wrapped
    in `<|START_TEXT|>...<|END_TEXT|>`, which json.loads() cannot parse.
    """
    _install(
        monkeypatch,
        _ok('<|START_TEXT|>{"answer": "x"}<|END_TEXT|>'),
    )
    assert await call_cohere_llm("q", 0.2, response_format="json_object") == '{"answer": "x"}'


# ---------------------------------------------------------------------------
# Response reading — where tolerance runs out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unrecognised_body_raises_rather_than_returning_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cohere_parse_client bug, not repeated.

    Until 2026-09-15 that client answered an unrecognised shape with a
    silently blank page: no exception, no metric, no fallback — a document
    that read as having no text on it. An empty string here would be
    indistinguishable from a model that had nothing to say.
    """
    _install(monkeypatch, lambda _r: httpx.Response(200, json={"unexpected": 1}))
    with pytest.raises(CohereResponseShapeError, match="unexpected"):
        await call_cohere_llm("q", 0.2)


@pytest.mark.asyncio
async def test_the_shape_error_names_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wire shape is UNVERIFIED, so this is the most likely way the module
    is wrong. The message has to say what to do about it."""
    _install(monkeypatch, lambda _r: httpx.Response(200, json={"nope": True}))
    with pytest.raises(CohereResponseShapeError, match="UNVERIFIED"):
        await call_cohere_llm("q", 0.2)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_forwards_every_delta_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The query path is SSE end to end and Laravel re-broadcasts each frame
    on Reverb, so a dropped delta is a silently truncated answer."""
    sent = _install(
        monkeypatch,
        _sse(
            json.dumps({"type": "content-delta", "delta": {"message": {"content": {"text": "Hello"}}}}),
            json.dumps({"type": "content-delta", "delta": {"message": {"content": {"text": " world"}}}}),
            "[DONE]",
        ),
    )
    deltas: list[str] = []
    answer = await call_cohere_llm("q", 0.2, token_callback=await _collect(deltas))

    assert sent[0]["stream"] is True
    assert deltas == ["Hello", " world"]
    assert answer == "Hello world"


@pytest.mark.asyncio
async def test_a_stream_request_is_only_made_when_someone_is_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(monkeypatch, _ok())
    await call_cohere_llm("q", 0.2)
    assert sent[0]["stream"] is False


@pytest.mark.asyncio
async def test_a_flat_delta_spelling_is_also_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _sse(json.dumps({"delta": {"text": "flat"}})))
    deltas: list[str] = []
    assert await call_cohere_llm("q", 0.2, token_callback=await _collect(deltas)) == "flat"


@pytest.mark.asyncio
async def test_a_malformed_frame_does_not_lose_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text that already arrived is still the answer."""
    _install(
        monkeypatch,
        _sse(
            json.dumps({"delta": {"text": "good"}}),
            "{not json",
            json.dumps({"delta": {"text": " text"}}),
        ),
    )
    deltas: list[str] = []
    assert await call_cohere_llm("q", 0.2, token_callback=await _collect(deltas)) == "good text"


@pytest.mark.asyncio
async def test_a_stream_that_yields_no_text_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streaming face of an unrecognised shape, and just as loud."""
    _install(monkeypatch, _sse(json.dumps({"type": "message-start", "id": "x"})))
    deltas: list[str] = []
    with pytest.raises(CohereResponseShapeError, match="_delta_text"):
        await call_cohere_llm("q", 0.2, token_callback=await _collect(deltas))


@pytest.mark.asyncio
async def test_usage_is_read_off_the_end_of_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.llm_calls import get_run_token_usage, reset_run_token_usage

    reset_run_token_usage()
    _install(
        monkeypatch,
        _sse(
            json.dumps({"delta": {"text": "hi"}}),
            json.dumps(
                {"type": "message-end", "delta": {"usage": {"tokens": {"input_tokens": 11, "output_tokens": 3}}}}
            ),
        ),
    )
    deltas: list[str] = []
    await call_cohere_llm("q", 0.2, token_callback=await _collect(deltas))
    assert get_run_token_usage() == (11, 3)


@pytest.mark.asyncio
async def test_usage_is_read_off_a_unary_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.llm_calls import get_run_token_usage, reset_run_token_usage

    reset_run_token_usage()
    _install(monkeypatch, _ok(input_tokens=7, output_tokens=2))
    await call_cohere_llm("q", 0.2)
    assert get_run_token_usage() == (7, 2)


@pytest.mark.asyncio
async def test_unreadable_usage_does_not_fail_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Usage is bookkeeping, not the answer. Zeros are the honest signal."""
    _install(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json={"message": {"content": [{"text": "fine"}]}, "usage": "not-a-dict"},
        ),
    )
    assert await call_cohere_llm("q", 0.2) == "fine"


# ---------------------------------------------------------------------------
# Retry posture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_status_is_retried_before_any_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"message": "overloaded"})
        return httpx.Response(200, json={"message": {"content": [{"text": "second try"}]}})

    _install(monkeypatch, _handler)
    assert await call_cohere_llm("q", 0.2) == "second try"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_a_failure_after_a_token_has_been_sent_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying mid-stream emits a second partial answer on top of the first.

    The user has already seen those tokens — Laravel broadcast them. The run
    has to degrade instead.
    """
    attempts = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"delta": {"text": "partial"}}\n\n',
        )

    _install(monkeypatch, _handler)

    async def _cb(_piece: str) -> None:
        raise RuntimeError("the socket went away")

    with pytest.raises(RuntimeError, match="socket went away"):
        await call_cohere_llm("q", 0.2, token_callback=_cb)
    assert attempts["n"] == 1, "a stream that reached the user must not be replayed"


@pytest.mark.asyncio
async def test_a_permanent_status_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, json={"message": "invalid api token"})

    _install(monkeypatch, _handler)
    with pytest.raises(httpx.HTTPStatusError):
        await call_cohere_llm("q", 0.2)
    assert attempts["n"] == 1


# ---------------------------------------------------------------------------
# Honesty ratchet
# ---------------------------------------------------------------------------


def test_the_module_still_says_its_wire_shape_is_unverified() -> None:
    """Delete this only alongside a committed probe report.

    ADR-0023 step 6 is "verify against the live API before cutover". Until
    that happens the docstring is the only thing telling the next reader that
    these field names are an educated guess.
    """
    assert llm_cohere.__doc__ is not None
    assert "[UNVERIFIED]" in llm_cohere.__doc__
