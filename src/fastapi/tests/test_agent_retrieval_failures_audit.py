"""Audit 2026-09-29, batch B: retrieval failures, secondary-tool gating,
data-source narrowing, citation pages and provider error codes.

RAG-12  a failed document search is not an empty corpus.
AGT-18  secondary tools gate on evidence count, not tool count.
AGT-16  every dispatchable tool honours data-source narrowing.
RAG-15  georag_chunks citations carry a page.
AGT-14  Cohere/Bedrock throttles and 5xx map to actionable error codes.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import (
    _with_retrieval_failures,
    assemble_node,
    execute_node,
)
from app.agent.agentic_retrieval.preprocessor import (
    TOOL_DATA_SOURCE_MAP,
    RetrievalFilters,
)
from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.errors import ErrorCode, RetrievalBackendUnavailable, classify_error
from app.agent.tools import DocumentChunk, DocumentSearchResult, _payload_page


class _Deps:
    project_id = "p-1"
    workspace_id = "ws-1"
    pg_pool = None
    redis_client = None


def _chunk(n: int) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"c{n}", text="grade text", source_document_id="d",
        document_title="R", section_number=None, section_title=None,
        section=None, page=1, document_type="NI43", report_id="r",
        relevance_score=0.9,
    )


def _state(intent: str = "factual_lookup", **extra: Any) -> AgenticRetrievalState:
    return AgenticRetrievalState(
        query="what is the resource estimate?", deps=_Deps(), intent=intent,
        effective_intent=intent, retrieval_profile=profile_for_intent(intent),
        **extra,
    )


def _patch_tools(monkeypatch, doc_result, calls: list[str]) -> None:
    async def fake_call(tool_name, query, deps):
        calls.append(tool_name)
        if tool_name == "search_documents":
            return doc_result
        return None

    monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)


# ---------------------------------------------------------------------------
# RAG-12
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sparse_encoder_unavailable", "error"])
async def test_backend_failure_fails_the_query(monkeypatch, failure):
    failed = DocumentSearchResult(
        chunks=[], count=0, data_source="Qdrant georag_chunks (x)",
        retrieval_failure=failure,
    )
    _patch_tools(monkeypatch, failed, [])
    with pytest.raises(RetrievalBackendUnavailable) as exc_info:
        await execute_node(_state())
    assert exc_info.value.reason == failure


@pytest.mark.asyncio
async def test_timeout_is_recorded_not_dropped(monkeypatch):
    timed_out = DocumentSearchResult(
        chunks=[], count=0, data_source="Qdrant georag_chunks (timeout)",
        retrieval_failure="timeout",
    )
    _patch_tools(monkeypatch, timed_out, [])
    update = await execute_node(_state())
    assert update["retrieval_failures"] == [
        "Documents (temporarily unavailable)",
    ]
    assert update["tool_results"] == []


@pytest.mark.asyncio
async def test_layer1_refusal_becomes_a_failure_when_search_did_not_run(monkeypatch):
    import app.agent.llm_calls as _llm_mod

    async def must_not_call(*a, **k):  # pragma: no cover
        raise AssertionError("LLM called with no evidence")

    monkeypatch.setattr(_llm_mod, "_call_llm", must_not_call)
    state = _state(
        tool_results=[],
        retrieval_failures=["Qdrant georag_chunks (timeout) via search_documents"],
    )
    with pytest.raises(RetrievalBackendUnavailable):
        await assemble_node(state)


def test_degraded_sources_gain_the_failed_search():
    from app.agent.response_assembler import assemble_response

    response = assemble_response("answer", []).model_copy(
        update={"degraded_sources": ["a via b"]},
    )
    out = _with_retrieval_failures(response, ["x via search_documents", "a via b"])
    assert out.degraded_sources == ["a via b", "x via search_documents"]


def test_retrieval_failure_classifies_as_retrieval_unavailable():
    code, message = classify_error(RetrievalBackendUnavailable("error"))
    assert code == ErrorCode.RETRIEVAL_UNAVAILABLE
    assert "Document search" in message


# ---------------------------------------------------------------------------
# AGT-18
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_well_covered_factual_lookup_skips_public_geoscience(monkeypatch):
    calls: list[str] = []
    docs = DocumentSearchResult(
        chunks=[_chunk(i) for i in range(5)], count=5, data_source="Qdrant",
    )
    _patch_tools(monkeypatch, docs, calls)
    await execute_node(_state())
    assert "search_public_geoscience" not in calls


@pytest.mark.asyncio
async def test_thin_factual_lookup_still_checks_public_geoscience(monkeypatch):
    calls: list[str] = []
    docs = DocumentSearchResult(chunks=[_chunk(1)], count=1, data_source="Qdrant")
    _patch_tools(monkeypatch, docs, calls)
    await execute_node(_state())
    assert "search_public_geoscience" in calls


# ---------------------------------------------------------------------------
# AGT-16
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    [
        "query_collar_details", "query_project_summary", "query_coverage_gap",
        "query_stereonet", "query_drill_traces_3d",
    ],
)
def test_public_geoscience_narrowing_excludes_internal_tools(tool):
    filters = RetrievalFilters(allowed_data_sources=frozenset({"public_geoscience"}))
    assert filters.is_tool_allowed(tool) is False


def test_every_profile_tool_has_a_data_source_mapping():
    from app.agent.agentic_retrieval.retrieval_profile import _PROFILES

    tools = {
        t for p in _PROFILES.values() for t in (*p.primary_tools, *p.secondary_tools)
    }
    tools |= {"query_collar_details", "query_stereonet", "query_drill_traces_3d"}
    assert tools - set(TOOL_DATA_SOURCE_MAP) == set()


# ---------------------------------------------------------------------------
# RAG-15
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"page": 7}, 7),
        ({"page_first": 12, "page_last": 13, "page_number": 12}, 12),
        ({"page_number": 4}, 4),
        ({"page_first": "9"}, 9),
        ({"page": None, "page_first": None}, None),
        ({}, None),
    ],
)
def test_payload_page(payload, expected):
    assert _payload_page(payload) == expected


# ---------------------------------------------------------------------------
# AGT-14
# ---------------------------------------------------------------------------


def test_cohere_throttle_is_rate_limited():
    from app.agent.llm_cohere import CoherePreStreamError

    code, _ = classify_error(CoherePreStreamError("HTTP 429 from Cohere before any output"))
    assert code == ErrorCode.RATE_LIMITED


def test_cohere_5xx_is_llm_unavailable():
    from app.agent.llm_cohere import CoherePreStreamError

    code, _ = classify_error(CoherePreStreamError("HTTP 503 from Cohere before any output"))
    assert code == ErrorCode.LLM_UNAVAILABLE


def test_cohere_shape_error_is_llm_unavailable():
    from app.agent.llm_cohere import CohereResponseShapeError

    code, _ = classify_error(CohereResponseShapeError("no text block"))
    assert code == ErrorCode.LLM_UNAVAILABLE


def test_bedrock_throttle_is_rate_limited():
    from app.agent.llm_bedrock import BedrockPreStreamError

    code, _ = classify_error(BedrockPreStreamError("ThrottlingException: slow down"))
    assert code == ErrorCode.RATE_LIMITED


def test_unrelated_runtime_error_is_still_internal():
    code, _ = classify_error(RuntimeError("HTTP 429 in some unrelated message"))
    assert code == ErrorCode.INTERNAL_ERROR
