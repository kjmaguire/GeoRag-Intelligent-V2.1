"""Audit 2026-10, LLM adapter findings 4, 6 and 7.

4  a generation cut off at the output cap (or errored mid-answer) was accepted
   as a finished answer: the adapters read the finish reason only to word the
   log line for an EMPTY answer.
6  the Bedrock / Anthropic / vLLM paths return "" when the model says nothing;
   assemble_response("") raised a ValidationError and the user saw
   INTERNAL_ERROR instead of the model_no_output refusal Cohere's path gets.
7  the pre-stream retry wall-clock budget restarted with every call, so retries
   could outlive the query's own deadline.

The adapters are driven through the same fakes the neighbouring adapter tests
use; nothing here reaches a model.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent import llm_cohere, llm_common
from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import assemble_node, validate_node
from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.hallucination.refusals import MODEL_NO_OUTPUT_TEXT
from app.agent.llm_calls import _llm_call_counter, llm_call_budget
from app.agent.llm_cohere import call_cohere_llm
from app.agent.llm_common import (
    BUDGET_EXHAUSTED_FALLBACK,
    is_truncated_finish_reason,
    note_truncated_generation,
    set_query_deadline,
    take_truncated_generation,
    wait_before_pre_stream_retry,
)
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.config import settings
from app.models.rag import Citation, GeoRAGResponse


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    """No real sleeps, a fresh call budget, and no truncation note left over."""
    delays: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(llm_common, "_sleep", _fake_sleep)
    take_truncated_generation()
    with llm_call_budget():
        yield delays
    take_truncated_generation()


# ---------------------------------------------------------------------------
# Finding 4 -- finish reasons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "reason",
    ["MAX_TOKENS", "max_tokens", "max-tokens", "length", "ERROR", "timeout",
     "model_context_window_exceeded", "content_filter", "content_filtered",
     "guardrail_intervened"],
)
def test_a_cut_off_finish_reason_is_recognised(reason: str) -> None:
    assert is_truncated_finish_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    ["COMPLETE", "STOP_SEQUENCE", "end_turn", "stop", "stop_sequence", "tool_use", "TOOL_CALL", "", None],
)
def test_a_normal_finish_reason_is_not(reason: str | None) -> None:
    assert is_truncated_finish_reason(reason) is False


def test_the_note_is_read_once() -> None:
    note_truncated_generation(backend="t", model="m", reason="MAX_TOKENS", answer_chars=10)

    assert take_truncated_generation() == "max_tokens"
    assert take_truncated_generation() is None


# --- Cohere ----------------------------------------------------------------
def _install_cohere(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm_cohere.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(settings, "LLM_BACKEND", "cohere")
    monkeypatch.setattr(settings, "COHERE_API_KEY", "test-only-not-a-real-key")


def _cohere_reply(text: str, finish_reason: str | None) -> Any:
    body: dict[str, Any] = {"message": {"content": [{"type": "text", "text": text}]}}
    if finish_reason is not None:
        body["finish_reason"] = finish_reason
    return lambda _request: httpx.Response(200, json=body)


def _cohere_stream(text: str, finish_reason: str) -> Any:
    frames = [
        {"type": "content-delta", "index": 0, "delta": {"message": {"content": {"text": text}}}},
        {"type": "message-end", "delta": {"finish_reason": finish_reason,
                                           "usage": {"tokens": {"input_tokens": 9, "output_tokens": 3}}}},
    ]
    return lambda _request: httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(f"data: {json.dumps(f)}\n\n" for f in frames).encode(),
    )


@pytest.mark.asyncio
async def test_cohere_marks_a_non_streamed_answer_cut_off_at_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_cohere(monkeypatch, _cohere_reply("The deepest hole reaches 51", "MAX_TOKENS"))

    text = await call_cohere_llm("q", 0.2)

    assert text == "The deepest hole reaches 51", "the delivered text is kept, not thrown away"
    assert take_truncated_generation() == "max_tokens"


@pytest.mark.asyncio
async def test_cohere_marks_a_streamed_answer_cut_off_at_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_cohere(monkeypatch, _cohere_stream("The deepest hole reaches 51", "MAX_TOKENS"))
    seen: list[str] = []

    async def _cb(piece: str) -> None:
        seen.append(piece)

    text = await call_cohere_llm("q", 0.2, token_callback=_cb)

    assert text == "The deepest hole reaches 51"
    assert take_truncated_generation() == "max_tokens"


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_cohere_reply, _cohere_stream], ids=["blocking", "streaming"])
async def test_cohere_leaves_a_complete_answer_alone(monkeypatch: pytest.MonkeyPatch, make: Any) -> None:
    _install_cohere(monkeypatch, make("A finished answer.", "COMPLETE"))

    async def _cb(piece: str) -> None:
        return None

    await call_cohere_llm("q", 0.2, token_callback=_cb if make is _cohere_stream else None)

    assert take_truncated_generation() is None


@pytest.mark.asyncio
async def test_a_reply_that_does_not_say_why_it_stopped_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_cohere(monkeypatch, _cohere_reply("An answer.", None))

    await call_cohere_llm("q", 0.2)

    assert take_truncated_generation() is None


@pytest.mark.asyncio
async def test_an_empty_answer_is_not_also_flagged_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty is the model_no_output path; a second caveat would contradict it."""
    body = {"message": {"content": [{"type": "thinking", "thinking": "hmm"}]}, "finish_reason": "MAX_TOKENS"}
    _install_cohere(monkeypatch, lambda _r: httpx.Response(200, json=body))

    assert await call_cohere_llm("q", 0.2) == BUDGET_EXHAUSTED_FALLBACK
    assert take_truncated_generation() is None


# --- Bedrock ---------------------------------------------------------------
class _BedrockClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    async def __aenter__(self) -> _BedrockClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def converse(self, **_request: Any) -> dict[str, Any]:
        return self._response


def _install_bedrock(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> None:
    import aioboto3

    class _Session:
        def client(self, *_a: Any, **_k: Any) -> _BedrockClient:
            return _BedrockClient(response)

    monkeypatch.setattr(aioboto3, "Session", _Session)
    monkeypatch.setattr(settings, "LLM_BACKEND", "bedrock")


def _converse(text: str, stop_reason: str) -> dict[str, Any]:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": 3, "outputTokens": 1},
        "stopReason": stop_reason,
    }


@pytest.mark.asyncio
async def test_bedrock_marks_an_answer_cut_off_at_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.llm_bedrock import call_bedrock_llm

    _install_bedrock(monkeypatch, _converse("The resource is 12", "max_tokens"))

    assert await call_bedrock_llm("q", 0.2) == "The resource is 12"
    assert take_truncated_generation() == "max_tokens"


@pytest.mark.asyncio
async def test_bedrock_leaves_end_turn_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.llm_bedrock import call_bedrock_llm

    _install_bedrock(monkeypatch, _converse("Done.", "end_turn"))

    await call_bedrock_llm("q", 0.2)

    assert take_truncated_generation() is None


# --- OpenAI-compatible (vLLM) ----------------------------------------------
def _openai_client(content: str, finish_reason: str | None) -> Any:
    choice: dict[str, Any] = {"message": {"content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    reply = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [choice], "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
    )

    async def _post(_url: str, **_kw: Any) -> Any:
        return reply

    return SimpleNamespace(post=_post)


@pytest.mark.asyncio
async def test_the_openai_compatible_path_marks_finish_reason_length(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.llm_calls import _call_openai_compatible_llm

    monkeypatch.setattr(settings, "LLM_BACKEND", "vllm")
    text = await _call_openai_compatible_llm(
        "hello", 0.0, base_url="http://vllm:8000/v1", model="m",
        http_client=_openai_client("The hole intersects", "length"), enable_thinking=False,
    )

    assert text == "The hole intersects"
    assert take_truncated_generation() == "length"


@pytest.mark.asyncio
async def test_the_openai_compatible_path_leaves_stop_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.llm_calls import _call_openai_compatible_llm

    monkeypatch.setattr(settings, "LLM_BACKEND", "vllm")
    await _call_openai_compatible_llm(
        "hello", 0.0, base_url="http://vllm:8000/v1", model="m",
        http_client=_openai_client("Complete.", "stop"), enable_thinking=False,
    )

    assert take_truncated_generation() is None


# --- Anthropic ---------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(("stop_reason", "expected"), [("max_tokens", "max_tokens"), ("end_turn", None)])
async def test_anthropic_marks_max_tokens(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str, expected: str | None
) -> None:
    from app.agent import egress_gate
    from app.agent.llm_calls import _call_anthropic_llm

    monkeypatch.setattr(egress_gate, "assert_external_llm_allowed", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "ANTHROPIC_ENABLE_PROMPT_CACHING", False)
    monkeypatch.setattr(settings, "ANTHROPIC_USE_PRIORITY_TIER", False)
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Hole PLS-22-08 reaches")], usage=None,
        stop_reason=stop_reason,
    )
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=message)))

    text = await _call_anthropic_llm("q", 0.1, client=client, workspace_id="w", pg_pool=None)

    assert text == "Hole PLS-22-08 reaches"
    assert take_truncated_generation() == expected


# --- the graph: assemble reads the note, validate says so ----------------------
class _Deps:
    openai_http_client: Any = None
    anthropic_client: Any = None
    pg_pool: Any = None
    neo4j_driver: Any = None
    redis_client: Any = None
    project_id = "00000000-0000-0000-0000-0000000000aa"
    workspace_id = "a0000000-0000-0000-0000-000000000001"


def _chunk() -> DocumentChunk:
    return DocumentChunk(
        chunk_id="c1", text="Resource 12.5 Mt", source_document_id="rep-1",
        document_title="Technical Report", section_number="14.1", section_title="Resource",
        section="14.1", page=3, document_type="NI43", report_id="rep-1", relevance_score=0.9,
    )


def _grounded_state() -> AgenticRetrievalState:
    docs = DocumentSearchResult(chunks=[_chunk()], count=1, data_source="qdrant (reranked)")
    return AgenticRetrievalState(query="what is the resource?", deps=_Deps()).model_copy(update={
        "intent": "factual_lookup",
        "effective_intent": "factual_lookup",
        "retrieval_profile": profile_for_intent("factual_lookup"),
        "tool_results": [("search_documents", docs)],
    })


@pytest.mark.asyncio
async def test_assemble_carries_the_truncation_note_onto_the_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.llm_calls as _llm_mod

    async def fake_call_llm(*_a: Any, **_k: Any) -> str:
        note_truncated_generation(backend="t", model="m", reason="MAX_TOKENS", answer_chars=40)
        return "The resource is 12.5 Mt [NI43-1] and the"

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    update = await assemble_node(_grounded_state())

    assert update["generation_truncated"] == "max_tokens"
    assert update["response"].text.startswith("The resource is 12.5 Mt")


@pytest.mark.asyncio
async def test_a_normal_assemble_leaves_the_state_field_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", AsyncMock(return_value="The resource is 12.5 Mt [NI43-1]."))

    update = await assemble_node(_grounded_state())

    assert "generation_truncated" not in update


def _validated(monkeypatch: pytest.MonkeyPatch, **state_fields: Any) -> Any:
    import app.agent.hallucination.orchestrator_validators as validators

    async def clean(resp: Any, tool_results: Any, deps: Any) -> Any:
        return resp, [], False

    monkeypatch.setattr(validators, "run_post_assembly_validation", clean)
    response = GeoRAGResponse(
        text="The resource is 12.5 Mt [NI43-1] and the",
        citations=[Citation(citation_id="[NI43-1]", citation_type="NI43", source_chunk_id="chunk-1",
                            document_title="Technical Report", relevance_score=0.9)],
        sources_used=["chunk-1"],
        confidence=0.9,
    )
    state = AgenticRetrievalState(query="q", deps=_Deps()).model_copy(
        update={"response": response, **state_fields}
    )
    return validate_node(state)


@pytest.mark.asyncio
async def test_validate_flags_an_answer_that_was_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    update = await _validated(monkeypatch, generation_truncated="max_tokens")

    response = update["response"]
    assert response.validation_state == "flagged"
    assert response.confidence <= 0.2
    assert "cut off" in response.text.split("\n\n", 1)[0]
    assert "The resource is 12.5 Mt" in response.text, "the delivered text is kept"
    assert any("Generation truncated" in w and "max_tokens" in w for w in update["validation_warnings"])


@pytest.mark.asyncio
async def test_validate_leaves_a_finished_answer_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    update = await _validated(monkeypatch)

    assert update["response"].validation_state == "clean"
    assert update["validation_warnings"] == []
    assert update["response"].confidence == pytest.approx(0.9)


def test_the_truncation_warning_is_not_filed_under_a_guard_code() -> None:
    from app.agent.guards import classify_guards

    warning = _nodes_mod._TRUNCATED_ANSWER_WARNING.format(reason="max_tokens")

    assert classify_guards(validation_warnings=[warning]) == []


# ---------------------------------------------------------------------------
# Finding 6 -- an empty answer is the model_no_output refusal on every backend
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["", "   \n\t ", BUDGET_EXHAUSTED_FALLBACK])
async def test_assemble_turns_no_content_into_the_model_no_output_refusal(
    monkeypatch: pytest.MonkeyPatch, empty: str
) -> None:
    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", AsyncMock(return_value=empty))

    response = (await assemble_node(_grounded_state()))["response"]

    assert response.text == MODEL_NO_OUTPUT_TEXT
    assert response.refusal_payload is not None
    assert response.refusal_payload["reason_code"] == "model_no_output"


@pytest.mark.asyncio
async def test_a_bedrock_reply_with_no_text_no_longer_crashes_response_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported failure end to end: Converse answers with an empty content
    list and no reasoning, call_bedrock_llm returns "", and assemble_response("")
    used to raise ValidationError (text min_length=1)."""
    _install_bedrock(monkeypatch, {"output": {"message": {"content": []}}, "stopReason": "end_turn",
                                   "usage": {"inputTokens": 5, "outputTokens": 0}})

    response = (await assemble_node(_grounded_state()))["response"]

    assert response.text == MODEL_NO_OUTPUT_TEXT
    assert response.refusal_payload["reason_code"] == "model_no_output"


@pytest.mark.asyncio
async def test_a_repair_reissue_that_says_nothing_still_restores_the_validated_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair re-issue shares the predicate; "" must raise, not assemble."""
    assert _nodes_mod._model_returned_nothing("") is True
    assert _nodes_mod._model_returned_nothing(None) is True
    assert _nodes_mod._model_returned_nothing(BUDGET_EXHAUSTED_FALLBACK) is True
    assert _nodes_mod._model_returned_nothing("A real answer.") is False


# ---------------------------------------------------------------------------
# Finding 7 -- the retry budget is the QUERY's, not the call's
# ---------------------------------------------------------------------------
@pytest.fixture
def deadline():
    """Publish a query deadline from inside a test.

    Called from the async test body, so it is set in that test's own Task
    context, which dies with the test: nothing to unpublish, and a reset token
    could not be used from the fixture's teardown context anyway.
    """

    def _set(seconds_from_now: float | None) -> None:
        deadline_at = None if seconds_from_now is None else time.monotonic() + seconds_from_now
        set_query_deadline(deadline_at)

    return _set


@pytest.mark.asyncio
async def test_a_late_call_does_not_get_a_fresh_gather_budget(
    monkeypatch: pytest.MonkeyPatch, _isolated: list[float], deadline: Any
) -> None:
    """170 s into a 180 s query a throttled call is told to wait 2 s or more.
    Before the fix it measured 180 s from ITS OWN first byte and said yes."""
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 180.0)
    deadline(1.0)  # 1 s left of the query

    ok = await wait_before_pre_stream_retry(
        label="t", attempt=1, max_retries=2, started_monotonic=time.monotonic()
    )

    assert ok is False
    assert _isolated == [], "no sleep was spent on a retry that cannot finish"
    assert _llm_call_counter.get() == 0, "and none was charged"


@pytest.mark.asyncio
async def test_a_retry_that_fits_the_query_deadline_still_happens(
    monkeypatch: pytest.MonkeyPatch, _isolated: list[float], deadline: Any
) -> None:
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 180.0)
    deadline(120.0)

    ok = await wait_before_pre_stream_retry(
        label="t", attempt=1, max_retries=2, started_monotonic=time.monotonic()
    )

    assert ok is True
    assert len(_isolated) == 1


@pytest.mark.asyncio
async def test_with_no_published_deadline_the_per_call_budget_alone_applies(
    monkeypatch: pytest.MonkeyPatch, _isolated: list[float], deadline: Any
) -> None:
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 180.0)
    deadline(None)

    ok = await wait_before_pre_stream_retry(
        label="t", attempt=1, max_retries=2, started_monotonic=time.monotonic()
    )

    assert ok is True


@pytest.mark.asyncio
async def test_the_openai_compatible_retry_loop_honours_the_query_deadline(
    monkeypatch: pytest.MonkeyPatch, deadline: Any
) -> None:
    import app.agent.llm_calls as llm_calls

    sleeps: list[float] = []

    async def _no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(llm_calls.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 180.0)
    boom = httpx.ConnectError("refused")
    calls = 0

    async def do_call() -> dict:
        nonlocal calls
        calls += 1
        raise llm_calls._PreStreamTransientError(boom)

    deadline(1.0)
    with pytest.raises(httpx.ConnectError):
        await llm_calls._retry_pre_stream_call(do_call, label="t")

    assert calls == 1, "the retry would not have fit before the query is cancelled"
    assert sleeps == []
