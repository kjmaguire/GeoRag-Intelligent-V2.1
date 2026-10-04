"""Audit 2026-10-04, guard-side items 2, 5, 6, 7, 8 and 9 of the query path.

Every test here pins a TIGHTENING: a refusal that used to ship as an answer,
a Layer 3 finding that used to cost only a x0.7, a hedge that used to launder
an uncited claim, a banner that named the wrong reason.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import (
    _banner_reason_for_triggers,
    _floor_confidence_with_warning_banner,
    assemble_node,
    validate_node,
)
from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.hallucination.claim_sentences import is_non_claim
from app.agent.hallucination.layer2_typed_output import (
    CITATION_REFUSAL_TEXT,
    enforce_claim_citations,
)
from app.agent.hallucination.orchestrator_validators import (
    LAYER3_CITED_ELSEWHERE_PREFIX,
    retry_trigger_warnings,
    run_post_assembly_validation,
    verify_cited_number_support,
)
from app.agent.hallucination.refusals import (
    MODEL_NO_OUTPUT_TEXT,
    PROVENANCE_REFUSAL_TEXT,
)
from app.agent.llm_common import BUDGET_EXHAUSTED_FALLBACK
from app.agent.response_assembler import (
    EMPTY_SOURCE_SENTINELS,
    _is_refusal,
    assemble_response,
    is_empty_source_id,
)
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.models.rag import Citation, GeoRAGResponse


class _Deps:
    openai_http_client: Any = None
    anthropic_client: Any = None
    pg_pool: Any = None
    neo4j_driver: Any = None
    redis_client: Any = None
    project_id = "00000000-0000-0000-0000-0000000000aa"
    workspace_id = "a0000000-0000-0000-0000-000000000001"


def _chunk(cid: str, text: str, *, score: float = 0.9) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=cid, text=text, source_document_id="rep-1",
        document_title="Technical Report", section_number="14.1",
        section_title="Resource", section="14.1", page=3, document_type="NI43",
        report_id="rep-1", relevance_score=score,
    )


def _docs(*chunks: DocumentChunk) -> DocumentSearchResult:
    return DocumentSearchResult(
        chunks=list(chunks), count=len(chunks), data_source="qdrant (reranked)",
    )


# ---------------------------------------------------------------------------
# Item 2 -- BUDGET_EXHAUSTED_FALLBACK never ships as an answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_text_becomes_a_model_no_output_refusal(monkeypatch) -> None:
    import app.agent.llm_calls as _llm_mod

    async def fake_call_llm(*a, **k):
        return BUDGET_EXHAUSTED_FALLBACK

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    state = AgenticRetrievalState(query="what is the resource?", deps=_Deps())
    state = state.model_copy(update={
        "intent": "factual_lookup",
        "effective_intent": "factual_lookup",
        "retrieval_profile": profile_for_intent("factual_lookup"),
        "tool_results": [("search_documents", _docs(_chunk("c1", "Resource 12.5 Mt")))],
    })
    update = await assemble_node(state)
    response = update["response"]

    assert response.text == MODEL_NO_OUTPUT_TEXT
    assert "token budget" not in response.text.lower()
    assert response.confidence <= 0.1
    assert response.refusal_payload is not None
    assert response.refusal_payload["reason_code"] == "model_no_output"
    assert "Layer" not in response.refusal_payload["message"]
    # Retrieved citations are dropped: only the inert placeholder remains.
    assert all(is_empty_source_id(c.source_chunk_id) for c in response.citations)
    assert not any(c.source_chunk_id.startswith("georag_reports") for c in response.citations)


def test_the_replacement_text_is_a_refusal_to_the_assembler() -> None:
    assert _is_refusal(MODEL_NO_OUTPUT_TEXT)
    # And the raw operator text, should it ever reach an assembler, too.
    assert _is_refusal(BUDGET_EXHAUSTED_FALLBACK)


@pytest.mark.asyncio
async def test_no_output_response_survives_validate_node_unbannered() -> None:
    response = assemble_response(MODEL_NO_OUTPUT_TEXT, [])
    state = AgenticRetrievalState(query="q", deps=_Deps()).model_copy(update={
        "response": response,
        "tool_results": [("search_documents", _docs(_chunk("c1", "Resource 12.5 Mt")))],
    })
    out = (await validate_node(state))["response"]
    assert out.text == MODEL_NO_OUTPUT_TEXT


# ---------------------------------------------------------------------------
# Item 5 -- Layer 3
# ---------------------------------------------------------------------------


def _answer(text: str, *chunk_texts: str) -> tuple[GeoRAGResponse, list[tuple[str, Any]]]:
    chunks = [_chunk(f"c{i}", t) for i, t in enumerate(chunk_texts, start=1)]
    results: list[tuple[str, Any]] = [("search_documents", _docs(*chunks))]
    return assemble_response(text, results), results


@pytest.mark.asyncio
async def test_a_single_ungrounded_number_now_sets_should_retry() -> None:
    response, results = _answer(
        "The hole returned 7.44 g/t Au over 12.6 m [NI43-1].",
        "The hole returned 1.85 g/t Au over 4.2 m.",
    )
    _resp, warnings, should_retry = await run_post_assembly_validation(
        response, results, _Deps()  # type: ignore[arg-type]
    )
    assert any(w.startswith("Layer 3: Ungrounded number") for w in warnings)
    assert should_retry is True
    assert retry_trigger_warnings(warnings)


@pytest.mark.asyncio
async def test_a_fully_grounded_answer_is_not_retried() -> None:
    response, results = _answer(
        "The hole returned 1.85 g/t Au over 4.2 m [NI43-1].",
        "The hole returned 1.85 g/t Au over 4.2 m.",
    )
    _resp, warnings, should_retry = await run_post_assembly_validation(
        response, results, _Deps()  # type: ignore[arg-type]
    )
    assert not [w for w in warnings if w.startswith("Layer 3")]
    assert should_retry is False


def test_number_cited_to_the_wrong_chunk_is_flagged_as_advisory() -> None:
    results = [("search_documents", _docs(
        _chunk("c1", "The mill is located 45 km from site."),
        _chunk("c2", "Indicated resource of 12.5 Mt at 1.85 g/t Au."),
    ))]
    warnings = verify_cited_number_support(
        "The indicated resource is 12.5 Mt [NI43-1].", results,
    )
    assert warnings == [
        f"{LAYER3_CITED_ELSEWHERE_PREFIX}12.5 cited to [NI43-1] appears only in [NI43-2]"
    ]
    assert warnings[0].startswith("Layer 3 advisory: number ")
    # Advisory: not a retry trigger.
    assert retry_trigger_warnings(warnings) == []


def test_number_cited_to_the_right_chunk_is_not_flagged() -> None:
    results = [("search_documents", _docs(
        _chunk("c1", "The mill is located 45 km from site."),
        _chunk("c2", "Indicated resource of 12.5 Mt at 1.85 g/t Au."),
    ))]
    assert verify_cited_number_support(
        "The indicated resource is 12.5 Mt [NI43-2].", results,
    ) == []


def test_a_number_in_no_evidence_is_left_to_the_whole_answer_check() -> None:
    results = [("search_documents", _docs(
        _chunk("c1", "Core recovery was good."),
        _chunk("c2", "Indicated resource of 12.5 Mt."),
    ))]
    assert verify_cited_number_support("Grade is 9.99 g/t [NI43-1].", results) == []


@pytest.mark.asyncio
async def test_advisory_wrong_chunk_finding_does_not_by_itself_force_retry() -> None:
    results = [("search_documents", _docs(
        _chunk("c1", "The mill is located 45 km from site."),
        _chunk("c2", "Indicated resource of 12.5 Mt at 1.85 g/t Au."),
    ))]
    response = assemble_response("The indicated resource is 12.5 Mt [NI43-1].", results)
    _r, warnings, should_retry = await run_post_assembly_validation(
        response, results, _Deps()  # type: ignore[arg-type]
    )
    assert any(w.startswith(LAYER3_CITED_ELSEWHERE_PREFIX) for w in warnings)
    assert not any("Ungrounded" in w for w in warnings)
    assert should_retry is False


# ---------------------------------------------------------------------------
# Item 6 -- a hedge no longer launders a claim
# ---------------------------------------------------------------------------


def test_hedge_clause_does_not_exempt_a_numeric_claim() -> None:
    sentence = (
        "The grade was 5.2 g/t Au over 3 m; passages do not cover the upper zone."
    )
    assert is_non_claim(sentence) is False


def test_pure_evidence_gap_statement_is_still_exempt() -> None:
    assert is_non_claim("The retrieved passages do not cover the upper zone.")
    assert is_non_claim("I don't have data for hole PLS-22-11.")
    assert is_non_claim("I don't have data for hole 36-1085.")


def test_enforce_claim_citations_drops_the_hedged_numeric_claim() -> None:
    response = GeoRAGResponse(
        text=(
            "Hole A returned 1.85 g/t Au over 4.2 m [NI43-1]. "
            "The grade was 5.2 g/t Au over 3 m; passages do not cover the upper zone."
        ),
        citations=[Citation(
            citation_id="[NI43-1]", citation_type="NI43",
            source_chunk_id="georag_reports:rep-1:section=1:chunk=c1",
            document_title="R", relevance_score=0.9,
        )],
        confidence=0.8, sources_used=["georag_reports:rep-1:section=1:chunk=c1"],
    )
    out, findings = enforce_claim_citations(response)
    assert findings
    assert "5.2" not in out.text
    assert "1.85" in out.text


# ---------------------------------------------------------------------------
# Item 7 -- the banner names the warning that triggered it
# ---------------------------------------------------------------------------


def test_banner_prefers_layer4_over_a_leading_layer1_advisory() -> None:
    reason = _banner_reason_for_triggers([
        "Layer 1: weak retrieval -- 2 chunk(s)",
        "Layer 3: Ungrounded number 4.2 in response",
        "Layer 4: Drill-hole ID 'DDH-999' not found in silver.collars",
    ])
    assert "hole" in reason
    assert "weak match" not in reason


@pytest.mark.parametrize(
    ("warnings", "fragment"),
    [
        (["Layer 6: grade 140% exceeds the physical maximum",
          "Layer 3: Ungrounded number 4.2"], "geological"),
        (["Layer 3: Ungrounded number 4.2", "Layer 5: citation c1 rejected"], "number"),
        (["Layer 5: citation c1 rejected", "Layer 2: claim removed"], "citation"),
        (["Layer 2: claim removed"], "no supporting source"),
    ],
)
def test_banner_priority_order(warnings: list[str], fragment: str) -> None:
    assert fragment in _banner_reason_for_triggers(warnings)


@pytest.mark.parametrize("warnings", [
    ["Layer 1: weak retrieval"],
    ["Completeness: uncited declarative sentence: x"],
    [],
])
def test_banner_never_names_layer1_or_completeness(warnings: list[str]) -> None:
    reason = _banner_reason_for_triggers(warnings)
    assert "weak match" not in reason
    assert reason == "part of the answer could not be checked against the sources"


@pytest.mark.asyncio
async def test_validate_node_banner_names_the_fabricated_hole_not_the_weak_match(
    monkeypatch,
) -> None:
    import app.agent.hallucination.layer5_provenance as _l5

    async def fake_validate(resp, tool_results, deps):
        return resp, [
            "Layer 1: weak retrieval -- 1 chunk(s) cleared the floor",
            "Layer 4: Drill-hole ID 'DDH-999' not found in silver.collars",
        ], True

    monkeypatch.setattr(
        "app.agent.hallucination.orchestrator_validators.run_post_assembly_validation",
        fake_validate,
    )

    async def fake_enrich(resp, pg_pool):
        return resp

    monkeypatch.setattr(_l5, "enrich_provenance", fake_enrich)
    response = GeoRAGResponse(
        text="Hole DDH-999 is 45 m deep. [DATA-1]",
        citations=[Citation(
            citation_id="[DATA-1]", citation_type="DATA",
            source_chunk_id="silver.collars:count=1:first=abc",
            document_title="Collars", relevance_score=0.9,
        )],
        confidence=0.9, sources_used=["silver.collars:count=1:first=abc"],
    )
    state = AgenticRetrievalState(query="q", deps=_Deps()).model_copy(
        update={"response": response}
    )
    out = (await validate_node(state))["response"]
    assert "could not be found in the project's records" in out.text
    assert "weak match" not in out.text


# ---------------------------------------------------------------------------
# Item 8 -- withheld answers: no "fact-checking flagged" banner, a payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    CITATION_REFUSAL_TEXT, PROVENANCE_REFUSAL_TEXT, MODEL_NO_OUTPUT_TEXT,
])
def test_no_banner_in_front_of_a_system_refusal(text: str) -> None:
    response = assemble_response(text, [])
    out = _floor_confidence_with_warning_banner(response, "a number could not be matched")
    assert out.text == text
    assert out.confidence <= 0.1


def test_banner_still_applies_to_a_model_written_answer() -> None:
    response = assemble_response("Hole A is 45 m deep.", [])
    out = _floor_confidence_with_warning_banner(response, "x")
    assert "automated fact-checking flagged" in out.text


@pytest.mark.asyncio
async def test_rule4_withheld_answer_carries_a_plain_refusal_payload() -> None:
    results = [("search_documents", _docs(_chunk("c1", "Resource of 12.5 Mt.")))]
    response = assemble_response(
        "The deposit hosts 12.5 Mt of mineralisation. The grade is 1.85 g/t Au.",
        results,
    )
    state = AgenticRetrievalState(query="q", deps=_Deps()).model_copy(update={
        "response": response, "tool_results": results,
    })
    out = (await validate_node(state))["response"]
    assert out.text == CITATION_REFUSAL_TEXT
    assert out.refusal_payload is not None
    assert out.refusal_payload["reason_code"] == "unsupported_by_sources"
    for word in ("Layer", "guard", "rule"):
        assert word not in out.refusal_payload["message"]
    assert "fact-checking" not in out.text.lower()


@pytest.mark.asyncio
async def test_provenance_withheld_answer_has_its_own_text_and_payload() -> None:
    results = [("search_documents", _docs(_chunk("real-chunk", "Resource of 12.5 Mt.")))]
    bad = Citation(
        citation_id="[NI43-1]", citation_type="NI43",
        source_chunk_id="georag_reports:rep-1:section=1:chunk=never-retrieved",
        document_title="R", relevance_score=0.9,
    )
    response = GeoRAGResponse(
        text="The resource is 12.5 Mt [NI43-1].", citations=[bad], confidence=0.9,
        sources_used=[bad.source_chunk_id],
    )
    state = AgenticRetrievalState(query="q", deps=_Deps()).model_copy(update={
        "response": response, "tool_results": results,
    })
    out = (await validate_node(state))["response"]
    assert out.text == PROVENANCE_REFUSAL_TEXT
    assert "no passages that cleared" not in out.text
    assert out.refusal_payload is not None
    assert out.refusal_payload["reason_code"] == "unsupported_by_sources"
    assert out.confidence <= 0.1


# ---------------------------------------------------------------------------
# Item 9 -- withheld-answer placeholders are empty sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentinel", ["citation-rejected", "provenance-rejected"])
def test_withheld_answer_placeholders_are_empty_sentinels(sentinel: str) -> None:
    assert sentinel in EMPTY_SOURCE_SENTINELS
    assert is_empty_source_id(sentinel)


def test_withheld_answer_persists_as_rejected_not_committed() -> None:
    from app.agent.guards import _drop_sentinel_citations

    for sentinel in ("citation-rejected", "provenance-rejected"):
        c = Citation(
            citation_id="[DATA-1]", citation_type="DATA", source_chunk_id=sentinel,
            document_title="x", relevance_score=0.0,
        )
        # persist_node: `rejected` when no real citation survives.
        assert _drop_sentinel_citations([c]) == []


def test_module_wiring_sanity() -> None:
    assert _nodes_mod._banner_reason_for_triggers is _banner_reason_for_triggers
