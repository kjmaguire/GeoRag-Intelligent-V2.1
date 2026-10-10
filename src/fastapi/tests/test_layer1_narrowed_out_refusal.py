"""A data-source selection that left nothing to search says so (2026-10-10 review, item 9).

``data_sources=["geophysics"]`` in Field mode (whose surfaces are drill logs,
assays and technical reports) narrows to nothing, and every tool is denied
(`RetrievalFilters.no_data_source_allowed`). The Layer 1 hard gate then refuses
-- correctly -- but its text said "Document search found no passages that
cleared the relevance threshold", although no search ran at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.agentic_retrieval.context_envelope import ContextEnvelope
from app.agent.agentic_retrieval.nodes import assemble_node
from app.agent.agentic_retrieval.preprocessor import preprocess_envelope
from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.hallucination.layer1_retrieval import (
    build_refusal_payload,
    build_refusal_text,
    refusal_texts,
)
from app.agent.hallucination.layer2_typed_output import _is_system_text
from app.agent.hallucination.orchestrator_validators import verify_completeness
from app.agent.orchestrator import _is_cacheable_response
from app.agent.response_assembler import _is_refusal


class _Deps:
    project_id = "p-1"
    workspace_id = "ws-1"
    pg_pool = None
    redis_client = None


def _state(envelope: ContextEnvelope | None, **extra: Any) -> AgenticRetrievalState:
    return AgenticRetrievalState(
        query="what do the airborne magnetics show?", deps=_Deps(), intent="factual_lookup",
        effective_intent="factual_lookup",
        retrieval_profile=profile_for_intent("factual_lookup"),
        retrieval_filters=preprocess_envelope(envelope) if envelope is not None else None,
        **extra,
    )


def _llm_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.llm_calls as _llm_mod

    async def must_not_call(*a: Any, **k: Any) -> str:  # pragma: no cover
        raise AssertionError("the LLM was called with no evidence")

    monkeypatch.setattr(_llm_mod, "_call_llm", must_not_call)


NARROWED_OUT = ContextEnvelope(mode="field", data_sources=["geophysics"])


@pytest.mark.asyncio
async def test_a_narrowing_that_left_nothing_does_not_claim_a_failed_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _llm_must_not_run(monkeypatch)
    statuses: list[str] = []

    async def status(message: str) -> None:
        statuses.append(message)

    update = await assemble_node(_state(NARROWED_OUT, tool_results=[], status_callback=status))
    response = update["response"]

    assert "relevance threshold" not in response.text
    assert "found no passages" not in response.text
    assert "excluded every source" in response.text and "nothing was searched" in response.text
    assert response.refusal_payload is not None
    assert response.refusal_payload["reason_code"] == "insufficient_evidence"
    assert "relevance threshold" not in response.refusal_payload["message"]
    assert "selected data sources" in response.refusal_payload["message"]
    assert statuses == ["The selected data sources exclude this question…"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope",
    [None, ContextEnvelope(), ContextEnvelope(mode="field", data_sources=["assays"])],
)
async def test_an_ordinary_empty_retrieval_keeps_its_text(
    monkeypatch: pytest.MonkeyPatch, envelope: ContextEnvelope | None
) -> None:
    """Nothing narrowed to nothing: the search ran and found nothing."""
    _llm_must_not_run(monkeypatch)
    update = await assemble_node(_state(envelope, tool_results=[]))
    response = update["response"]
    assert response.text == build_refusal_text()
    assert "relevance threshold" in response.text
    assert response.refusal_payload == build_refusal_payload()


def test_the_narrowed_out_text_is_a_refusal_everywhere_a_refusal_is_recognised() -> None:
    text = build_refusal_text(narrowed_out=True)
    assert text != build_refusal_text()
    assert text in refusal_texts() and build_refusal_text() in refusal_texts()
    assert _is_refusal(text)  # the assembler floors its confidence
    assert _is_system_text(text)  # the rule-4 enforcer does not strip it
    assert verify_completeness(text) == []  # and it is not "uncited claims"


def test_the_narrowed_out_text_is_never_replayed_from_the_cache() -> None:
    from app.models.rag import Citation, GeoRAGResponse

    citation = Citation(
        citation_id="[DATA-1]", citation_type="DATA", source_chunk_id="x:1",
        document_title="Collars", relevance_score=0.9,
    )
    for narrowed_out in (False, True):
        response = GeoRAGResponse(
            text=build_refusal_text(narrowed_out=narrowed_out), citations=[citation],
            confidence=0.1, sources_used=["x:1"],
        )
        assert _is_cacheable_response(response) is False


def test_the_payload_carries_the_same_reason_code_the_chat_branches_on() -> None:
    narrowed = build_refusal_payload(narrowed_out=True)
    ordinary = build_refusal_payload()
    assert narrowed["reason_code"] == ordinary["reason_code"] == "insufficient_evidence"
    assert {k for k in narrowed} == {k for k in ordinary}
    assert narrowed["message"] != ordinary["message"]
