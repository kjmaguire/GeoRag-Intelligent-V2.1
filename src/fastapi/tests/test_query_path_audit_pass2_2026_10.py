"""Audit 2026-10-04, second pass over the FastAPI query path (items A-K).

A  a repair re-issue that got no model output must not replace the answer
B  hosted backend + no reranker object fails closed; lifespan re-raises a
   retired-backend error
D  response-assembler titles carry no store or vendor names
E  only a DOCUMENT-search failure turns a Layer 1 refusal into an outage
F  the per-sentence Layer 3 advisory takes no demotion; dead escalation gone
G  qwen3_causal has its own probability-scale floor (tests in
   test_rerank_failure_is_hard.py cover the tool; this file the config)
H  _measured_holes uses hole_id_key (PLS-2-28 is not PLS-22-8)
I  a missing cols key cannot skip usage metering
K  an assay result's LIMIT-capped sample size is not a statable number
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import (
    RepairReissueNoOutputError,
    assemble_node,
    execute_node,
    repair_shadow_node,
)
from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.errors import RetrievalBackendUnavailable
from app.agent.hallucination.refusals import (
    MODEL_NO_OUTPUT_MESSAGE,
    MODEL_NO_OUTPUT_TEXT,
    make_refusal_payload,
)
from app.agent.llm_common import BUDGET_EXHAUSTED_FALLBACK
from app.agent.tools import (
    AssayDataResult,
    AssaySample,
    CollarRecord,
    DocumentChunk,
    DocumentSearchResult,
    SpatialQueryResult,
    query_assay_data,
)
from app.config import settings as _settings
from app.models.rag import Citation, GeoRAGResponse

PROJECT = "00000000-0000-0000-0000-0000000000bb"
WORKSPACE = "a0000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# A -- repair re-issue with no model output
# ---------------------------------------------------------------------------


class _RepairDeps:
    project_id = "p"
    workspace_id = "ws-1"
    pg_pool = None
    openai_http_client = None
    anthropic_client = None


def _response(text: str) -> GeoRAGResponse:
    return GeoRAGResponse(
        text=text,
        citations=[Citation(
            citation_id="[DATA-1]",
            source_chunk_id="00000000-0000-0000-0000-000000000001",
            document_title="T", relevance_score=0.9, citation_type="DATA",
        )],
        confidence=0.7,
        sources_used=["00000000-0000-0000-0000-000000000001"],
    )


def _repair_state(**overrides: Any) -> AgenticRetrievalState:
    base = AgenticRetrievalState(
        query="q", deps=_RepairDeps(), intent="synthesis", effective_intent="synthesis",
        tool_results=[("query_project_overview", {"count": 1})],
        response=_response("original validated answer [DATA-1]"),
        validation_warnings=["layer 3: ungrounded number 5.0"],
    )
    return base.model_copy(update=overrides)


@pytest.mark.asyncio
async def test_reissue_llm_only_raises_on_a_budget_exhausted_reply(monkeypatch) -> None:
    import app.agent.llm_calls as _llm_mod

    async def fake_call_llm(*a: Any, **k: Any) -> str:
        return BUDGET_EXHAUSTED_FALLBACK

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    state = _repair_state()
    with pytest.raises(RepairReissueNoOutputError):
        await _nodes_mod._reissue_llm_only(state, "Be careful.")
    assert state.response.text == "original validated answer [DATA-1]"


@pytest.mark.asyncio
async def test_repair_loop_keeps_the_validated_answer_when_stage3_gets_no_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_FULL_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_TERMINAL_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 1, raising=False)
    import app.agent.llm_calls as _llm_mod

    async def fake_call_llm(*a: Any, **k: Any) -> str:
        return BUDGET_EXHAUSTED_FALLBACK

    async def must_not_validate(state: Any) -> Any:  # pragma: no cover
        raise AssertionError("a no-output re-issue reached validate")

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    monkeypatch.setattr(_nodes_mod, "validate_node", must_not_validate)

    state = _repair_state()
    update = await repair_shadow_node(state)

    assert "response" not in update
    assert state.response.text == "original validated answer [DATA-1]"
    assert state.response.confidence == 0.7


@pytest.mark.asyncio
async def test_stage4_model_no_output_refusal_does_not_replace_the_answer(
    monkeypatch,
) -> None:
    from app.agent.agentic_retrieval import preprocessor as _pp_mod

    async def fake_execute(s: Any) -> dict[str, Any]:
        return {"tool_results": [("query_project_overview", {"count": 2})],
                "evidence_packet": None}

    async def fake_assemble(s: Any) -> dict[str, Any]:
        refusal = _response(MODEL_NO_OUTPUT_TEXT).model_copy(update={
            "refusal_payload": make_refusal_payload(
                "model_no_output", MODEL_NO_OUTPUT_MESSAGE,
            ),
        })
        return {"response": refusal}

    monkeypatch.setattr(_nodes_mod, "execute_node", fake_execute)
    monkeypatch.setattr(_nodes_mod, "assemble_node", fake_assemble)
    state = _repair_state(
        retrieval_profile=profile_for_intent("synthesis"),
        retrieval_filters=_pp_mod.preprocess_envelope(None),
    )
    with pytest.raises(RepairReissueNoOutputError):
        await _nodes_mod._reissue_retrieval(state, {})
    # The raise happens before any response is adopted.
    assert state.response.text == "original validated answer [DATA-1]"


# ---------------------------------------------------------------------------
# B -- reranker is None on a hosted backend; lifespan retired-backend raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_reranker_block_reraises_a_retired_backend(monkeypatch) -> None:
    from types import SimpleNamespace

    import app.services.reranker as _rr
    from app.main import _init_reranker
    from app.services._bedrock import RetiredAzureConfiguration

    def boom() -> None:
        raise RetiredAzureConfiguration("RERANKER_BACKEND='foundry' is retired")

    monkeypatch.setattr(_rr, "get_reranker_or_none", boom)
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(RetiredAzureConfiguration):
        _init_reranker(app)  # type: ignore[arg-type]


def test_lifespan_reranker_block_still_swallows_an_ordinary_failure(monkeypatch) -> None:
    from types import SimpleNamespace

    import app.services.reranker as _rr
    from app.main import _init_reranker

    def boom() -> None:
        raise RuntimeError("model download failed")

    monkeypatch.setattr(_rr, "get_reranker_or_none", boom)
    app = SimpleNamespace(state=SimpleNamespace())
    _init_reranker(app)  # type: ignore[arg-type]
    # None, not absent: a hosted backend then fails document search closed.
    assert app.state.reranker is None
    assert app.state.reranker_version is None


# ---------------------------------------------------------------------------
# D -- titles carry no store or vendor names
# ---------------------------------------------------------------------------


def _collar(i: int) -> CollarRecord:
    return CollarRecord(
        collar_id=f"c-{i}", hole_id=f"H-{i:03d}", easting=1.0, northing=2.0,
        elevation=3.0, total_depth=100.0, hole_type="Diamond", azimuth=0.0,
        dip=-60.0, status="Done", drill_date=None, longitude=-106.0, latitude=52.0,
    )


def test_collar_title_uses_the_matching_total_and_plain_words() -> None:
    from app.agent.response_assembler import _extract_document_title

    result = SpatialQueryResult(
        collars=[_collar(i) for i in range(50)], count=50,
        data_source="PostGIS silver.collars", total_count=567,
    )
    title = _extract_document_title("query_spatial_collars", result)
    assert title == "Drill collars (567 holes)"
    assert "PostGIS" not in title


def test_collar_title_falls_back_to_the_row_count_without_a_total() -> None:
    from app.agent.response_assembler import _extract_document_title

    three = SpatialQueryResult(
        collars=[_collar(i) for i in range(3)], count=3, data_source="x",
    )
    one = SpatialQueryResult(collars=[_collar(0)], count=1, data_source="x")
    assert _extract_document_title("t", three) == "Drill collars (3 holes)"
    assert _extract_document_title("t", one) == "Drill collars (1 hole)"


def test_empty_document_search_title_names_no_vector_store() -> None:
    from app.agent.response_assembler import _extract_document_title

    empty = DocumentSearchResult(chunks=[], count=0, data_source="Qdrant georag_chunks")
    assert _extract_document_title("search_documents", empty) == "Document search (no results)"


def test_no_title_branch_names_a_store_or_vendor() -> None:
    import inspect

    from app.agent import response_assembler

    source = inspect.getsource(response_assembler._extract_document_title)
    for name in ("PostGIS", "Qdrant", "Neo4j", "Cohere", "Bedrock"):
        assert name not in source, name


# ---------------------------------------------------------------------------
# E -- only a document-search failure makes a Layer 1 refusal an outage
# ---------------------------------------------------------------------------


class _Deps:
    project_id = "p-1"
    workspace_id = "ws-1"
    pg_pool = None
    redis_client = None


def _node_state(**extra: Any) -> AgenticRetrievalState:
    return AgenticRetrievalState(
        query="how many holes?", deps=_Deps(), intent="factual_lookup",
        effective_intent="factual_lookup",
        retrieval_profile=profile_for_intent("factual_lookup"), **extra,
    )


def _llm_must_not_run(monkeypatch) -> None:
    import app.agent.llm_calls as _llm_mod

    async def must_not_call(*a: Any, **k: Any) -> str:  # pragma: no cover
        raise AssertionError("the LLM was called with no evidence")

    monkeypatch.setattr(_llm_mod, "_call_llm", must_not_call)


@pytest.mark.asyncio
async def test_structured_failure_plus_layer1_refusal_degrades_not_raises(monkeypatch) -> None:
    _llm_must_not_run(monkeypatch)
    state = _node_state(
        tool_results=[],
        retrieval_failures=["Drill-hole collars (temporarily unavailable)"],
        document_search_failed=False,
    )
    update = await assemble_node(state)
    response = update["response"]
    assert response.refusal_payload is not None
    assert "Drill-hole collars (temporarily unavailable)" in response.degraded_sources


@pytest.mark.asyncio
async def test_document_failure_plus_layer1_refusal_still_raises(monkeypatch) -> None:
    _llm_must_not_run(monkeypatch)
    state = _node_state(
        tool_results=[],
        retrieval_failures=["Documents (temporarily unavailable)"],
        document_search_failed=True,
    )
    with pytest.raises(RetrievalBackendUnavailable):
        await assemble_node(state)


@pytest.mark.asyncio
async def test_execute_node_flags_only_a_document_search_failure(monkeypatch) -> None:
    structured = AssayDataResult(
        samples=[], count=0, element="", available_elements=[], min_value=None,
        max_value=None, mean_value=None, median_value=None,
        data_source="PostGIS silver.samples", retrieval_failure="timeout",
    )
    doc_timeout = DocumentSearchResult(
        chunks=[], count=0, data_source="Qdrant georag_chunks (timeout)",
        retrieval_failure="timeout",
    )

    def patch_tools(doc: Any, other: Any) -> None:
        async def fake_call(tool_name: str, query: str, deps: Any) -> Any:
            return doc if tool_name == "search_documents" else (
                other if tool_name.startswith("query_") else None
            )

        monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)

    def two_tool_state() -> AgenticRetrievalState:
        state = _node_state()
        assert state.retrieval_profile is not None
        state.retrieval_profile = state.retrieval_profile.model_copy(
            update={"primary_tools": ["search_documents", "query_assay_data"]},
        )
        return state

    patch_tools(None, structured)
    update = await execute_node(two_tool_state())
    assert update["retrieval_failures"], "the structured failure is still reported"
    assert update["document_search_failed"] is False

    patch_tools(doc_timeout, None)
    update = await execute_node(two_tool_state())
    assert update["document_search_failed"] is True


# ---------------------------------------------------------------------------
# F -- Layer 3: dead escalation gone; the advisory takes no demotion
# ---------------------------------------------------------------------------


def test_cited_elsewhere_advisory_is_not_a_layer3_demotion_trigger() -> None:
    from app.agent.confidence_computer import _is_layer3_warning
    from app.agent.hallucination.orchestrator_validators import (
        LAYER3_CITED_ELSEWHERE_PREFIX,
        _severity_buckets,
        retry_trigger_warnings,
    )

    advisory = f"{LAYER3_CITED_ELSEWHERE_PREFIX}12.5 cited to [NI43-1] appears only in [NI43-2]"
    assert advisory.startswith("Layer 3 advisory:")
    assert not _is_layer3_warning(advisory)
    # ... while a genuine Layer 3 finding still is one.
    assert _is_layer3_warning("Layer 3: Ungrounded number 4.2 in response")
    assert _is_layer3_warning("Layer 3 tuple: value 5.2 reported as 'ppm'")
    critical, high, adv = _severity_buckets([advisory])
    assert (critical, high, adv) == ([], [], [])
    assert retry_trigger_warnings([advisory]) == []


def test_advisory_alone_leaves_confidence_untouched() -> None:
    from app.agent.confidence_computer import apply_guard_demotion
    from app.agent.hallucination.orchestrator_validators import LAYER3_CITED_ELSEWHERE_PREFIX

    response = _response("The resource is 12.5 Mt [DATA-1].")
    out, reasons = apply_guard_demotion(
        response, [f"{LAYER3_CITED_ELSEWHERE_PREFIX}12.5 cited to [NI43-1] appears only in [NI43-2]"],
    )
    assert reasons == []
    assert out.confidence == response.confidence


def test_advisory_still_maps_to_the_numbers_banner_reason() -> None:
    from app.agent.agentic_retrieval.nodes import _banner_reason, _banner_reason_for_triggers
    from app.agent.hallucination.orchestrator_validators import LAYER3_CITED_ELSEWHERE_PREFIX

    warning = f"{LAYER3_CITED_ELSEWHERE_PREFIX}12.5 cited to [NI43-1] appears only in [NI43-2]"
    expected = "a number in the answer could not be matched to the source documents"
    assert _banner_reason(warning) == expected
    assert _banner_reason_for_triggers([warning]) == expected


def test_a_genuine_layer3_finding_still_demotes() -> None:
    from app.agent.confidence_computer import (
        NUMERIC_FLAG_CONFIDENCE_FACTOR,
        apply_guard_demotion,
    )

    response = _response("The resource is 99 Mt [DATA-1].")
    out, reasons = apply_guard_demotion(response, ["Layer 3: Ungrounded number 99 in response"])
    assert reasons
    assert out.confidence == round(0.7 * NUMERIC_FLAG_CONFIDENCE_FACTOR, 4)


def test_the_inert_layer3_escalation_is_gone() -> None:
    import inspect

    from app.agent.hallucination import orchestrator_validators as ov

    source = inspect.getsource(ov.run_post_assembly_validation)
    for dead in ("_layer3_escalated_high", "_layer3_escalated_critical", "NUMERIC_RETRY_THRESHOLD"):
        assert dead not in source, dead
    assert not hasattr(_settings, "NUMERIC_RETRY_THRESHOLD")


@pytest.mark.asyncio
async def test_any_layer3_finding_still_forces_retry() -> None:
    from app.agent.hallucination.orchestrator_validators import run_post_assembly_validation
    from app.agent.response_assembler import assemble_response

    chunk = DocumentChunk(
        chunk_id="c1", text="Indicated resource of 12.5 Mt.", source_document_id="d",
        document_title="R", section_number=None, section_title=None, section=None,
        page=1, document_type="NI43", report_id="r", relevance_score=0.9,
    )
    results = [("search_documents", DocumentSearchResult(chunks=[chunk], count=1, data_source="x"))]
    response = assemble_response("Grade is 977.123 g/t [NI43-1].", results)
    deps = MagicMock()
    deps.pg_pool = None
    _r, warnings, should_retry = await run_post_assembly_validation(response, results, deps)
    assert any(w.startswith("Layer 3:") for w in warnings)
    assert should_retry is True


# ---------------------------------------------------------------------------
# G -- config knob
# ---------------------------------------------------------------------------


def test_probability_threshold_setting_exists_with_a_probability_default() -> None:
    value = _settings.RERANKER_SCORE_THRESHOLD_PROBABILITY
    assert value == 0.2
    assert 0.0 < value < 1.0


# ---------------------------------------------------------------------------
# H -- _measured_holes keys with hole_id_key
# ---------------------------------------------------------------------------


def test_measured_holes_does_not_merge_pls_2_28_with_pls_22_8() -> None:
    from app.agent.hallucination.orchestrator_validators import (
        _measured_holes,
        _not_in_evidence_warning,
    )
    from app.agent.hole_id_patterns import hole_id_key

    answer = "PLS-2-28 intersected 7.4 g/t Au over 2 m."
    hole_ids = ["PLS-2-28", "PLS-22-8"]
    measured = _measured_holes(answer, hole_ids)
    assert measured == {hole_id_key("PLS-2-28")}
    assert hole_id_key("PLS-22-8") not in measured

    # The neighbour that only got named in passing is advisory, not critical.
    assert _not_in_evidence_warning("PLS-22-8", answer, hole_ids).startswith("Layer 4 advisory:")
    assert _not_in_evidence_warning("PLS-2-28", answer, hole_ids).startswith("Layer 4: Drill-hole ID")


def test_measured_holes_still_merges_separator_variants_of_one_hole() -> None:
    from app.agent.hallucination.orchestrator_validators import _measured_holes
    from app.agent.hole_id_patterns import hole_id_key

    assert _measured_holes("BH-12 returned 3.1 g/t.", ["BH-12"]) == {hole_id_key("BH-12")}


# ---------------------------------------------------------------------------
# I -- a missing cols key cannot skip usage metering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_followups_meters_usage_even_when_cols_lacks_the_schema_version(
    monkeypatch,
) -> None:
    usage = AsyncMock()
    monkeypatch.setattr(_nodes_mod, "_write_chat_usage_event", usage)
    monkeypatch.setattr(
        _nodes_mod, "_persist_retrieval_and_citation_items",
        AsyncMock(return_value=(0, 0)),
    )
    state = AgenticRetrievalState(query="q", deps=_Deps(), intent="factual_lookup")
    await _nodes_mod._persist_followups(
        state,
        pg_pool=MagicMock(),
        workspace_id="ws-1",
        answer_run_id="run-1",
        have_row=True,
        model_id="m",
        backend="cohere",
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        response_confidence=0.5,
        cols={},  # no "answer_schema_version"
    )
    usage.assert_awaited_once()


# ---------------------------------------------------------------------------
# K -- assay evidence: the LIMIT-capped sample size is not a statable number
# ---------------------------------------------------------------------------


class _Txn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _assay_ctx(conn: Any) -> Any:
    from dataclasses import dataclass

    from app.agent.deps import AgentDeps

    conn.transaction = MagicMock(return_value=_Txn())
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    deps = AgentDeps(
        pg_pool=pool, qdrant_client=None, neo4j_driver=None,  # type: ignore[arg-type]
        project_id=PROJECT, embedding_model=None, reranker=None,
        workspace_id=WORKSPACE,
    )

    @dataclass
    class _C:
        deps: AgentDeps

    return _C(deps=deps)


@pytest.mark.asyncio
async def test_query_assay_data_exposes_the_matching_total() -> None:
    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "jsonb_object_keys" in sql:
            return [{"elem": "Au_ppb_k"}]
        return [
            {"hole_id": f"H-{i}", "collar_id": f"c{i}", "from_depth": 1.0,
             "to_depth": 2.0, "sample_type": "core", "val": 9.5 - i * 0.01}
            for i in range(50)
        ]

    async def fetchrow(sql: str, *args: Any) -> dict[str, Any]:
        return {"min_v": 0.1, "max_v": 9.5, "mean_v": 1.2, "std_v": 0.8,
                "median_v": 1.0, "total_n": 4000}

    conn = AsyncMock()
    conn.fetch = fetch
    conn.fetchrow = fetchrow
    result = await query_assay_data(_assay_ctx(conn), project_id=PROJECT)
    assert len(result.samples) == 50
    assert result.total_count == 4000


def _assay_result(n_samples: int, total: int | None) -> AssayDataResult:
    return AssayDataResult(
        samples=[
            AssaySample(
                hole_id=f"H-{i}", collar_id=f"c{i}", from_depth=1.5, to_depth=2.5,
                element="Au_ppb", value=11.5 + i, sample_type="core",
            )
            for i in range(n_samples)
        ],
        count=total if total is not None else n_samples,
        element="Au_ppb", available_elements=["Au_ppb"],
        min_value=0.1, max_value=88.8, mean_value=4.4, median_value=3.3,
        data_source="PostGIS silver.samples", total_count=total,
    )


def test_layer3_grounds_the_assay_total_but_not_the_sample_size() -> None:
    from app.agent.hallucination.orchestrator_validators import _collect_evidence

    result = _assay_result(37, 4000)
    literal = _collect_evidence([("query_assay_data", result)]).literal
    assert 4000.0 in literal
    # 37 is len(samples) -- the LIMIT-capped plot subset, not a fact about the
    # project. (The sample VALUES are 11.5..47.5, none of which is 37.0.)
    assert 37.0 not in literal


def test_assay_result_without_a_total_still_grounds_its_row_count() -> None:
    from app.agent.hallucination.orchestrator_validators import _collect_evidence

    result = _assay_result(37, None)
    assert 37.0 in _collect_evidence([("query_assay_data", result)]).literal
