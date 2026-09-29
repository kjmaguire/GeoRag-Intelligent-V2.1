"""Audit 2026-09-29, batch B: LLM call budget, response_format, query cache.

AGT-12  MAX_LLM_CALLS_PER_QUERY spans every graph node of a run.
AGT-13  response_format "json" and "json_object" both reach each transport
        in the spelling it reads.
AGT-7   only clean, cited, complete answers are cached, without their
        answer_run_id.
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from typing import Any

import pytest

import app.agent.llm_calls as llm_calls
from app.agent.llm_calls import _llm_call_counter, llm_call_budget
from app.config import settings

# ---------------------------------------------------------------------------
# AGT-12
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_is_shared_by_tasks_inside_a_run():
    async def one_call() -> None:
        _llm_call_counter.set(_llm_call_counter.get() + 1)

    with llm_call_budget():
        # Each create_task copies the context, exactly as LangGraph runs nodes.
        await asyncio.create_task(one_call())
        await asyncio.create_task(one_call())
        await asyncio.create_task(one_call())
        assert _llm_call_counter.get() == 3


@pytest.mark.asyncio
async def test_each_run_starts_at_zero():
    with llm_call_budget():
        _llm_call_counter.set(7)
    with llm_call_budget():
        assert _llm_call_counter.get() == 0


@pytest.mark.asyncio
async def test_cap_trips_across_nodes(monkeypatch):
    """Cap 2: two node Tasks spend it, the third raises."""
    monkeypatch.setattr(settings, "MAX_LLM_CALLS_PER_QUERY", 2, raising=False)
    monkeypatch.setattr(settings, "LLM_BACKEND", "vllm", raising=False)

    async def fake_openai(*args, **kwargs):
        return "ok"

    monkeypatch.setattr(llm_calls, "_call_openai_compatible_llm", fake_openai)

    async def node() -> str:
        return await llm_calls._call_llm(query="q", context="c")

    with llm_call_budget():
        await asyncio.create_task(node())
        await asyncio.create_task(node())
        with pytest.raises(llm_calls.LLMCallBudgetExceeded):
            await asyncio.create_task(node())


def test_a_run_count_is_unbound_when_the_run_ends():
    """Nothing after the run - nor any context copied after it - inherits it."""
    with llm_call_budget():
        _llm_call_counter.set(5)
    assert llm_calls._llm_call_cell.get() is None
    assert contextvars.copy_context().run(_llm_call_counter.get) == 0


def test_nested_runs_are_independent_and_restore_the_outer_count():
    with llm_call_budget():
        _llm_call_counter.set(3)
        with llm_call_budget():
            assert _llm_call_counter.get() == 0
            _llm_call_counter.set(6)
        assert _llm_call_counter.get() == 3


@pytest.mark.asyncio
async def test_outside_a_run_the_count_is_context_local():
    """The pre-AGT-12 behaviour: a child Task's increments stay in the child."""
    ctx = contextvars.copy_context()

    async def body() -> tuple[int, int]:
        _llm_call_counter.set(2)

        async def child() -> int:
            _llm_call_counter.set(_llm_call_counter.get() + 5)
            return _llm_call_counter.get()

        seen_in_child = await asyncio.create_task(child())
        return seen_in_child, _llm_call_counter.get()

    seen_in_child, seen_in_parent = await asyncio.get_running_loop().create_task(
        body(), context=ctx
    )
    assert (seen_in_child, seen_in_parent) == (7, 2)
    assert llm_calls._llm_call_cell.get() is None


def test_run_agentic_retrieval_opens_a_budget_around_the_graph():
    import inspect

    from app.agent.agentic_retrieval import graph

    src = inspect.getsource(graph.run_agentic_retrieval)
    assert src.index("with llm_call_budget():") < src.index("graph.ainvoke(")


# ---------------------------------------------------------------------------
# AGT-13
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["json", "json_object", "JSON"])
@pytest.mark.parametrize(
    ("backend", "expected"),
    [("cohere", "json_object"), ("bedrock", "json_object"), ("vllm", "json")],
)
async def test_response_format_is_normalised(monkeypatch, spelling, backend, expected):
    seen: dict[str, Any] = {}

    async def fake_chat(*args, **kwargs):
        seen["rf"] = kwargs.get("response_format")
        return "{}"

    monkeypatch.setattr(settings, "LLM_BACKEND", backend, raising=False)
    monkeypatch.setattr(settings, "MAX_LLM_CALLS_PER_QUERY", 99, raising=False)
    import app.agent.llm_bedrock as _bedrock
    import app.agent.llm_cohere as _cohere

    monkeypatch.setattr(_cohere, "call_cohere_llm", fake_chat)
    monkeypatch.setattr(_bedrock, "call_bedrock_llm", fake_chat)
    monkeypatch.setattr(llm_calls, "_call_openai_compatible_llm", fake_chat)
    with llm_call_budget():
        await llm_calls._call_llm(query="q", context="c", response_format=spelling)
    assert seen["rf"] == expected


@pytest.mark.asyncio
async def test_no_response_format_stays_none(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_chat(*args, **kwargs):
        seen["rf"] = kwargs.get("response_format")
        return "text"

    monkeypatch.setattr(settings, "LLM_BACKEND", "cohere", raising=False)
    import app.agent.llm_cohere as _cohere

    monkeypatch.setattr(_cohere, "call_cohere_llm", fake_chat)
    with llm_call_budget():
        await llm_calls._call_llm(query="q", context="c")
    assert seen["rf"] is None


# ---------------------------------------------------------------------------
# AGT-7
# ---------------------------------------------------------------------------


def _answer(**update: Any):
    from app.models.rag import Citation, GeoRAGResponse

    base = GeoRAGResponse(
        text="The collar is at 402 m [DATA-1].",
        citations=[Citation(
            citation_id="[DATA-1]", citation_type="DATA",
            source_chunk_id="00000000-0000-0000-0000-000000000001",
            document_title="T", relevance_score=0.9,
        )],
        confidence=0.9,
        sources_used=["00000000-0000-0000-0000-000000000001"],
        validation_state="clean",
        answer_run_id=uuid.uuid4(),
    )
    return base.model_copy(update=update)


def test_clean_cited_answer_is_cacheable():
    from app.agent.orchestrator import _is_cacheable_response

    assert _is_cacheable_response(_answer()) is True


@pytest.mark.parametrize(
    "update",
    [
        {"validation_state": "flagged"},
        {"validation_state": "unverified"},
        {"refusal_payload": {"type": "refusal"}},
        {"degraded_sources": ["Qdrant (timeout) via search_documents"]},
        {"citations": []},
    ],
)
def test_unclean_answers_are_not_cached(update):
    from app.agent.orchestrator import _is_cacheable_response

    assert _is_cacheable_response(_answer(**update)) is False


def test_refusal_and_budget_apology_are_not_cached():
    from app.agent.hallucination.layer1_retrieval import build_refusal_text
    from app.agent.llm_common import BUDGET_EXHAUSTED_FALLBACK
    from app.agent.orchestrator import _is_cacheable_response

    assert _is_cacheable_response(_answer(text=build_refusal_text())) is False
    assert _is_cacheable_response(_answer(text=BUDGET_EXHAUSTED_FALLBACK)) is False


@pytest.mark.asyncio
async def test_cached_payload_drops_answer_run_id(monkeypatch):
    from app.agent import orchestrator
    from app.agent.agentic_retrieval import graph

    stored: dict[str, str] = {}

    class _Redis:
        async def get(self, key):
            return None

        async def setex(self, key, ttl, value):
            stored[key] = value

    answer = _answer()

    async def fake_run(*args, **kwargs):
        return answer

    monkeypatch.setattr(graph, "run_agentic_retrieval", fake_run)
    import app.agent.agentic_retrieval as _ar

    monkeypatch.setattr(_ar, "run_agentic_retrieval", fake_run)
    monkeypatch.setattr(
        orchestrator, "_query_response_cache_key", lambda deps, q: "k",
    )

    class _Deps:
        redis_client = _Redis()
        project_id = "p"

    result = await orchestrator.run_deterministic_rag("q", _Deps())
    assert result.answer_run_id == answer.answer_run_id  # the live run keeps it
    assert '"answer_run_id":null' in stored["k"].replace(" ", "")
