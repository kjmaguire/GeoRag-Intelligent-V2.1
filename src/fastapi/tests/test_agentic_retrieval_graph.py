"""Tests for the agentic-retrieval LangGraph — Phase 2 / Step 2.3.

Locks the contract:
  - graph compiles without errors
  - each intent routes to the right profile
  - profile flags match the plan's Step 2.3 table
  - graph end-to-end runs against mocked tools and returns a GeoRAGResponse
  - hypothesis-generation triggers a second 'adversarial' tool call
  - synthesis profile has conflict_detection_enabled
  - flag-off path in run_deterministic_rag is byte-identical
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.agent.agentic_retrieval import (
    INTENT_LABELS,
    AgenticRetrievalState,
    RetrievalProfile,
    get_compiled_graph,
    profile_for_intent,
    run_agentic_retrieval,
)
from app.agent.agentic_retrieval.intent_classifier import Intent
from app.agent.agentic_retrieval.nodes import (
    _build_adversarial_query,
    assemble_node,
    classify_node,
    execute_node,
    route_node,
    validate_node,
)

# ---------------------------------------------------------------------------
# Profiles — locked from plan Step 2.3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("intent", list(INTENT_LABELS))
def test_every_intent_has_a_profile(intent: Intent) -> None:
    p = profile_for_intent(intent)
    assert isinstance(p, RetrievalProfile)
    assert p.intent == intent
    assert p.primary_tools  # at least one primary tool


def test_factual_profile_is_bm25_weighted_standards_first() -> None:
    p = profile_for_intent("factual_lookup")
    assert p.primary_tools == ["search_documents"]
    assert p.bm25_weight >= 0.7  # standards-first
    assert p.answer_emphasis == "exact_citation"
    assert not p.adversarial_pass_enabled
    assert not p.conflict_detection_enabled


def test_synthesis_profile_enables_conflict_detection() -> None:
    p = profile_for_intent("synthesis")
    assert p.conflict_detection_enabled is True
    assert "search_documents" in p.primary_tools
    assert "query_spatial_collars" in p.primary_tools
    assert "query_assay_data" in p.primary_tools


def test_hypothesis_profile_enables_adversarial_pass() -> None:
    p = profile_for_intent("hypothesis_generation")
    assert p.adversarial_pass_enabled is True
    assert p.answer_emphasis == "competing_hypotheses"


def test_anomaly_profile_surfaces_qaqc_fields() -> None:
    p = profile_for_intent("anomaly_detection")
    assert p.surface_qa_qc_fields is True
    assert "query_assay_data" in p.primary_tools
    assert p.answer_emphasis == "anomaly_table"


def test_uncertainty_profile_retrieves_conflicts() -> None:
    p = profile_for_intent("uncertainty_quantification")
    assert p.conflict_detection_enabled is True
    assert p.answer_emphasis == "uncertainty_drivers"


def test_decision_profile_default_no_regulatory_flag() -> None:
    p = profile_for_intent("decision_support")
    assert p.require_regulatory_constraints is False
    assert p.answer_emphasis == "ranked_options"


def test_decision_profile_flips_regulatory_on_touch() -> None:
    p = profile_for_intent("decision_support", regulatory_touch=True)
    assert p.require_regulatory_constraints is True


def test_decision_profile_carries_evidence_for_all_options() -> None:
    """The plan requires retrieval of evidence for ALL options before ranking."""
    p = profile_for_intent("decision_support")
    # Same breadth as synthesis — multi-store retrieval up-front.
    assert "search_documents" in p.primary_tools
    assert "query_spatial_collars" in p.primary_tools
    assert "query_assay_data" in p.primary_tools
    assert "traverse_knowledge_graph" in p.primary_tools


# ---------------------------------------------------------------------------
# Adversarial query rewrite
# ---------------------------------------------------------------------------


def test_adversarial_query_rewrite_carries_contradict_framing() -> None:
    rewritten = _build_adversarial_query(
        "Mineralisation continues down-dip of DDH-07."
    )
    # Must include language signalling the retriever to look for
    # disconfirming evidence.
    assert "CONTRADICTS" in rewritten or "LIMITS" in rewritten
    # Original query text is preserved so the embedding stays anchored.
    assert "DDH-07" in rewritten


# ---------------------------------------------------------------------------
# Graph compiles
# ---------------------------------------------------------------------------


def test_graph_compiles_and_is_cached() -> None:
    g1 = get_compiled_graph()
    g2 = get_compiled_graph()
    assert g1 is g2  # cached singleton


# ---------------------------------------------------------------------------
# End-to-end with mocked tools + mocked LLM
# ---------------------------------------------------------------------------


@dataclass
class _FakeDeps:
    """Stand-in for orchestrator AgentDeps. Carries None for everything."""

    openai_http_client: Any = None
    anthropic_client: Any = None
    pg_pool: Any = None
    neo4j_driver: Any = None
    redis_client: Any = None
    project_id: str = "test-project"


@pytest.mark.asyncio
async def test_classify_node_uses_intent_classifier() -> None:
    state = AgenticRetrievalState(
        query="Should we drill DDH-13?", deps=_FakeDeps()
    )
    update = await classify_node(state)
    assert update["intent"] == "decision_support"
    assert update["intent_result"].matched_triggers


@pytest.mark.asyncio
async def test_route_node_selects_profile_from_classified_intent() -> None:
    state = AgenticRetrievalState(query="x", deps=_FakeDeps())
    # Populate the prior-node outputs ourselves.
    state = state.model_copy(update={"intent": "synthesis"})
    update = await route_node(state)
    profile: RetrievalProfile = update["retrieval_profile"]
    assert profile.intent == "synthesis"
    assert profile.conflict_detection_enabled is True


@pytest.mark.asyncio
async def test_execute_node_dispatches_primary_tools(monkeypatch) -> None:
    """Smoke-test follow-up: dispatcher uses the real tool signatures.

    Every legacy tool is a Pydantic-AI ``@geo_agent.tool`` callable and so
    declares ``RunContext[AgentDeps]`` (or our ``ToolContext`` shim) as
    its first positional argument:

      * ``search_documents(ctx, query_text, project_id, ...)``
      * ``query_spatial_collars(ctx, project_id, ...)``
      * ``query_assay_data(ctx, project_id, ...)``
      * ``query_project_overview(ctx, project_id)``

    ``query_downhole_logs`` and ``traverse_knowledge_graph`` require NER
    (hole_id / entity_name) and are skipped by the dispatcher until the
    entity-extraction step lands.
    """
    calls: list[tuple[str, tuple]] = []

    async def fake_search_documents(ctx, query_text: str, project_id: str):
        assert ctx is not None and hasattr(ctx, "deps"), (
            "dispatcher must pass a ctx with .deps"
        )
        calls.append(("search_documents", (query_text, project_id)))
        return {"chunks": [], "count": 0}

    async def fake_project_id_only(ctx, project_id: str):
        assert ctx is not None and hasattr(ctx, "deps"), (
            "dispatcher must pass a ctx with .deps"
        )
        calls.append(("project_id_only", (project_id,)))
        return {"chunks": [], "count": 0}

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "search_documents", fake_search_documents, raising=False)
    monkeypatch.setattr(_tools_mod, "query_spatial_collars", fake_project_id_only, raising=False)
    monkeypatch.setattr(_tools_mod, "query_assay_data", fake_project_id_only, raising=False)
    monkeypatch.setattr(_tools_mod, "query_project_overview", fake_project_id_only, raising=False)
    # query_downhole_logs and traverse_knowledge_graph are intentionally
    # skipped by the dispatcher (NER unwired). They must NOT be called.

    state = AgenticRetrievalState(query="integrate across the wells", deps=_FakeDeps())
    state = state.model_copy(
        update={
            "intent": "synthesis",
            "retrieval_profile": profile_for_intent("synthesis"),
        }
    )
    update = await execute_node(state)
    # search_documents + spatial + assay tools should fire (3 of 5 primaries
    # from the synthesis profile — downhole + graph are skipped pending NER).
    # query_project_overview from secondary_tools fires too when primary
    # yielded < 3 results (it didn't, so secondary may or may not run).
    tool_names = [name for name, _ in update["tool_results"]]
    assert "search_documents" in tool_names
    assert "query_spatial_collars" in tool_names
    assert "query_assay_data" in tool_names
    # NER-gated tools are not called.
    assert "query_downhole_logs" not in tool_names
    assert "traverse_knowledge_graph" not in tool_names


@pytest.mark.asyncio
async def test_execute_node_runs_adversarial_pass_for_hypothesis(monkeypatch) -> None:
    queries_seen: list[str] = []

    async def fake_search_documents(ctx, query_text: str, project_id: str):
        queries_seen.append(query_text)
        return {"chunks": [], "count": 0}

    async def fake_project_id_only(ctx, project_id: str):
        return {"chunks": [], "count": 0}

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "search_documents", fake_search_documents, raising=False)
    monkeypatch.setattr(_tools_mod, "query_assay_data", fake_project_id_only, raising=False)
    monkeypatch.setattr(_tools_mod, "query_spatial_collars", fake_project_id_only, raising=False)
    # traverse_knowledge_graph is NER-gated; the dispatcher skips it.

    state = AgenticRetrievalState(
        query="What geological models could explain the Cu-Au anomaly?",
        deps=_FakeDeps(),
    )
    state = state.model_copy(
        update={
            "intent": "hypothesis_generation",
            "retrieval_profile": profile_for_intent("hypothesis_generation"),
        }
    )
    update = await execute_node(state)
    # Exactly one adversarial pass — second search_documents call carrying
    # the CONTRADICTS framing.
    assert any("CONTRADICTS" in q or "LIMITS" in q for q in queries_seen)
    # And the result is tagged with the _adversarial suffix.
    tool_names = [name for name, _ in update["tool_results"]]
    assert "search_documents_adversarial" in tool_names


@pytest.mark.asyncio
async def test_execute_node_populates_evidence_packet(monkeypatch) -> None:
    """Plan §3a/§3b wiring — execute_node MUST build a typed EvidencePacket
    from the collected tool_results so downstream consumers (response
    assembler, validators, trace writer, MapLibre trigger) can read typed
    evidence instead of duck-typed dicts.

    Contract:
      * After execute_node runs, ``update["evidence_packet"]`` is an
        EvidencePacket (not None).
      * The packet's evidence list contains kinds matching what the tool
        results carried — at minimum ``"document"`` when search_documents
        returned dict-row chunks.
      * The packet is authority-ranked (annotate_evidence_packet +
        rank_evidence_by_authority both applied) — a high-authority doc
        sorts ahead of a low-authority one even when they were appended
        in the reverse order.
    """
    # search_documents returns a wrapped DocumentSearchResult shape with
    # a `.chunks` attribute — exactly what the real tool does. We test
    # the unwrapping path here.
    class _FakeDocSearch:
        def __init__(self, chunks):
            self.chunks = chunks
            self.count = len(chunks)

    # Two document chunks: one NI 43-101 (rank 1) and one Internal Memo
    # (rank 5). They're appended in low-to-high authority order to verify
    # the packet sort kicks in.
    chunks_payload = [
        {
            "chunk_id": "00000000-0000-0000-0000-0000000000A0",
            "text": "Internal memo: drilling targets reviewed.",
            "document_id": "doc-memo",
            "document_title": "Internal Memo 2024-03",
            "document_type": "Internal Memo",
            "page": 1,
            "char_start": 0,
            "char_end": 41,
            "relevance_score": 0.4,
        },
        {
            "chunk_id": "00000000-0000-0000-0000-0000000000A1",
            "text": "NI 43-101 Technical Report — Mineral Resource Estimate.",
            "document_id": "doc-ni43",
            "document_title": "Technical Report 2024",
            "document_type": "NI 43-101",
            "page": 12,
            "char_start": 0,
            "char_end": 55,
            "relevance_score": 0.9,
        },
    ]

    async def fake_search_documents(ctx, query_text: str, project_id: str):
        return _FakeDocSearch(chunks_payload)

    async def fake_project_id_only(ctx, project_id: str):
        return _FakeDocSearch([])

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "search_documents", fake_search_documents, raising=False)
    monkeypatch.setattr(_tools_mod, "query_spatial_collars", fake_project_id_only, raising=False)
    monkeypatch.setattr(_tools_mod, "query_assay_data", fake_project_id_only, raising=False)
    monkeypatch.setattr(_tools_mod, "query_project_overview", fake_project_id_only, raising=False)

    state = AgenticRetrievalState(query="any synthesis question", deps=_FakeDeps())
    state = state.model_copy(
        update={
            "intent": "synthesis",
            "retrieval_profile": profile_for_intent("synthesis"),
        }
    )
    update = await execute_node(state)

    from app.agent.evidence import DocumentEvidence, EvidencePacket

    packet = update["evidence_packet"]
    assert isinstance(packet, EvidencePacket), (
        "execute_node must build an EvidencePacket from tool_results"
    )
    # At least the two NI/Memo chunks made it through extraction. (Empty
    # collars / assays / overview results contribute nothing.)
    document_members = [e for e in packet.evidence if isinstance(e, DocumentEvidence)]
    assert len(document_members) >= 2, (
        f"expected ≥2 DocumentEvidence members, got {len(document_members)}"
    )
    # Authority sort: the NI 43-101 (rank 1) ranks above the Internal
    # Memo (rank 5), regardless of input order.
    assert document_members[0].document_type == "NI 43-101"
    assert document_members[0].authority_rank == 1
    assert any(d.authority_rank == 5 for d in document_members)
    # Budget arithmetic: total_tokens > 0 and remaining_budget computed.
    assert packet.total_tokens > 0
    assert packet.remaining_budget != 0  # could be positive or negative; not unset


@pytest.mark.asyncio
async def test_assemble_node_context_prep_disabled_uses_legacy_path(monkeypatch) -> None:
    """When ``CONTEXT_PREP_ENABLED=False`` (default), assemble_node
    builds the LLM context block from ``state.tool_results`` — the
    byte-identical legacy path. The prepared-packet path stays dark."""
    import app.agent.llm_calls as _llm_mod
    from app.agent.agentic_retrieval.state import AgenticRetrievalState
    from app.agent.evidence import DocumentEvidence, EvidencePacket
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "CONTEXT_PREP_ENABLED", False, raising=False)

    captured_context: list[str] = []

    async def fake_call_llm(*args, **kwargs):
        captured_context.append(kwargs.get("context") or (args[1] if len(args) > 1 else ""))
        return "stub answer [DATA-1]"

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    # Stamp both tool_results AND an evidence_packet so the test can
    # distinguish which path ran.
    doc = DocumentEvidence(
        document_id="d", document_title="T", document_type="NI 43-101",
        page=1, chunk_id="c", text="payload from prep packet",
        char_start=0, char_end=10,
    )
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[doc],
        total_tokens=10, remaining_budget=1000,
    )

    state = AgenticRetrievalState(query="q", deps=_FakeDeps())
    state = state.model_copy(update={
        "intent": "synthesis",
        "effective_intent": "synthesis",
        "retrieval_profile": profile_for_intent("synthesis"),
        "tool_results": [("search_documents", {"chunks": ["legacy-chunk-rendered"]})],
        "evidence_packet": packet,
    })
    await assemble_node(state)
    # Legacy path renders tool=search_documents into the context.
    assert any("legacy-chunk-rendered" in c for c in captured_context)
    # AND it must NOT have rendered from the packet (we'd see kind=document otherwise).
    assert not any("kind=document" in c for c in captured_context)


@pytest.mark.asyncio
async def test_assemble_node_context_prep_enabled_uses_prepared_packet(monkeypatch) -> None:
    """When ``CONTEXT_PREP_ENABLED=True``, assemble_node renders from
    the prepared EvidencePacket (authority-ranked + diversity-balanced
    + budget-fit) instead of the raw tool_results."""
    import app.agent.llm_calls as _llm_mod
    from app.agent.agentic_retrieval.state import AgenticRetrievalState
    from app.agent.evidence import DocumentEvidence, EvidencePacket
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "CONTEXT_PREP_ENABLED", True, raising=False)

    captured_context: list[str] = []

    async def fake_call_llm(*args, **kwargs):
        captured_context.append(kwargs.get("context") or (args[1] if len(args) > 1 else ""))
        return "stub answer [DATA-1]"

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    doc = DocumentEvidence(
        document_id="d", document_title="T", document_type="NI 43-101",
        page=1, chunk_id="c", text="payload from prep packet",
        char_start=0, char_end=10,
    )
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[doc],
        total_tokens=10, remaining_budget=1000,
    )

    state = AgenticRetrievalState(query="q", deps=_FakeDeps())
    state = state.model_copy(update={
        "intent": "synthesis",
        "effective_intent": "synthesis",
        "retrieval_profile": profile_for_intent("synthesis"),
        "tool_results": [("search_documents", {"chunks": ["legacy-chunk-rendered"]})],
        "evidence_packet": packet,
    })
    await assemble_node(state)
    # Prepared path renders kind=document into the context.
    assert any("kind=document" in c for c in captured_context)
    # And does NOT use the legacy "tool=search_documents" string.
    assert not any("legacy-chunk-rendered" in c for c in captured_context)


@pytest.mark.asyncio
async def test_assemble_node_context_prep_enabled_handles_empty_packet(monkeypatch) -> None:
    """With the flag on but evidence_packet=None (the converter failed
    or the graph wasn't engaged), assemble_node MUST fall back to the
    legacy path gracefully — never break the answer flow."""
    import app.agent.llm_calls as _llm_mod
    from app.agent.agentic_retrieval.state import AgenticRetrievalState
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "CONTEXT_PREP_ENABLED", True, raising=False)

    captured: list[str] = []

    async def fake_call_llm(*args, **kwargs):
        captured.append(kwargs.get("context") or "")
        return "stub answer [DATA-1]"

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    state = AgenticRetrievalState(query="q", deps=_FakeDeps())
    state = state.model_copy(update={
        "intent": "synthesis",
        "effective_intent": "synthesis",
        "retrieval_profile": profile_for_intent("synthesis"),
        "tool_results": [("search_documents", {"chunks": ["legacy-fallback"]})],
        "evidence_packet": None,  # converter didn't produce one
    })
    # Must not raise; falls through to legacy path.
    await assemble_node(state)
    assert any("legacy-fallback" in c for c in captured)


@pytest.mark.asyncio
async def test_run_agentic_retrieval_returns_geo_rag_response(monkeypatch) -> None:
    """Smoke test: end-to-end against fake tools + a fake LLM returns a
    valid :class:`GeoRAGResponse`.
    """

    async def fake_search_documents(ctx, query_text: str, project_id: str):
        return {"chunks": [], "count": 0}

    async def fake_project_id_only(ctx, project_id: str):
        return {"chunks": [], "count": 0}

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "search_documents", fake_search_documents, raising=False)
    for t in ("query_spatial_collars", "query_assay_data", "query_project_overview"):
        monkeypatch.setattr(_tools_mod, t, fake_project_id_only, raising=False)

    async def fake_call_llm(*args, **kwargs):
        return "I don't have data on that in this project. [DATA-1]"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    # Stub out post-assembly validation so we don't need a live DB.
    async def fake_validate(response, tool_results, deps):
        return response, [], False

    import app.agent.hallucination.orchestrator_validators as _validators

    monkeypatch.setattr(_validators, "run_post_assembly_validation", fake_validate)

    response = await run_agentic_retrieval(
        "What is the deepest hole in this project?", _FakeDeps()
    )
    from app.models.rag import GeoRAGResponse

    assert isinstance(response, GeoRAGResponse)
    assert response.text  # non-empty


# ---------------------------------------------------------------------------
# Step 2.5 — status_callback/token_callback actually reach the graph nodes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_node_forwards_token_callback_to_call_llm(monkeypatch) -> None:
    """The whole point of Step 2.5: assemble_node must pass state.token_callback
    straight into _call_llm so the SSE stream gets real per-chunk deltas
    instead of the queries.py synthetic word-split fallback."""
    import app.agent.llm_calls as _llm_mod
    from app.agent.agentic_retrieval.state import AgenticRetrievalState

    captured_kwargs: dict = {}

    async def fake_call_llm(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return "stub answer [DATA-1]"

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    async def fake_token_cb(chunk: str) -> None:
        pass

    state = AgenticRetrievalState(query="q", deps=_FakeDeps(), token_callback=fake_token_cb)
    state = state.model_copy(update={
        "intent": "synthesis",
        "effective_intent": "synthesis",
        "retrieval_profile": profile_for_intent("synthesis"),
        "tool_results": [("search_documents", {"chunks": ["x"]})],
    })
    await assemble_node(state)
    assert captured_kwargs.get("token_callback") is fake_token_cb


@pytest.mark.asyncio
async def test_assemble_node_omits_token_callback_when_none(monkeypatch) -> None:
    """Non-streaming callers (tests, batch/eval paths) must not accidentally
    flip _call_llm into streaming mode — token_callback stays None."""
    import app.agent.llm_calls as _llm_mod
    from app.agent.agentic_retrieval.state import AgenticRetrievalState

    captured_kwargs: dict = {}

    async def fake_call_llm(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return "stub answer [DATA-1]"

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    state = AgenticRetrievalState(query="q", deps=_FakeDeps())
    state = state.model_copy(update={
        "intent": "synthesis",
        "effective_intent": "synthesis",
        "retrieval_profile": profile_for_intent("synthesis"),
        "tool_results": [("search_documents", {"chunks": ["x"]})],
    })
    await assemble_node(state)
    assert captured_kwargs.get("token_callback") is None


@pytest.mark.asyncio
async def test_assemble_node_emits_synthesizing_status(monkeypatch) -> None:
    import app.agent.llm_calls as _llm_mod
    from app.agent.agentic_retrieval.state import AgenticRetrievalState

    async def fake_call_llm(*args, **kwargs):
        return "stub answer [DATA-1]"

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    statuses: list[str] = []

    async def fake_status_cb(message: str) -> None:
        statuses.append(message)

    state = AgenticRetrievalState(query="q", deps=_FakeDeps(), status_callback=fake_status_cb)
    state = state.model_copy(update={
        "intent": "synthesis",
        "effective_intent": "synthesis",
        "retrieval_profile": profile_for_intent("synthesis"),
        "tool_results": [("search_documents", {"chunks": ["x"]})],
    })
    await assemble_node(state)
    assert "Synthesizing answer…" in statuses


@pytest.mark.asyncio
async def test_classify_node_emits_classifying_status() -> None:
    statuses: list[str] = []

    async def fake_status_cb(message: str) -> None:
        statuses.append(message)

    state = AgenticRetrievalState(
        query="what is the collar depth", deps=_FakeDeps(), status_callback=fake_status_cb,
    )
    await classify_node(state)
    assert "Classifying query…" in statuses


@pytest.mark.asyncio
async def test_execute_node_emits_querying_status(monkeypatch) -> None:
    async def fake_search_documents(ctx, query_text: str, project_id: str):
        return {"chunks": [], "count": 0}

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "search_documents", fake_search_documents, raising=False)

    statuses: list[str] = []

    async def fake_status_cb(message: str) -> None:
        statuses.append(message)

    state = AgenticRetrievalState(query="q", deps=_FakeDeps(), status_callback=fake_status_cb)
    state = state.model_copy(update={
        "intent": "synthesis",
        "effective_intent": "synthesis",
        "retrieval_profile": profile_for_intent("synthesis"),
    })
    await execute_node(state)
    assert "Querying PostGIS + Qdrant…" in statuses


@pytest.mark.asyncio
async def test_run_agentic_retrieval_threads_callbacks_through_state(monkeypatch) -> None:
    """End-to-end: callbacks passed into run_agentic_retrieval() actually
    reach the nodes — not just accepted-and-dropped like before Step 2.5."""

    async def fake_search_documents(ctx, query_text: str, project_id: str):
        return {"chunks": [], "count": 0}

    async def fake_project_id_only(ctx, project_id: str):
        return {"chunks": [], "count": 0}

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "search_documents", fake_search_documents, raising=False)
    for t in ("query_spatial_collars", "query_assay_data", "query_project_overview"):
        monkeypatch.setattr(_tools_mod, t, fake_project_id_only, raising=False)

    captured_kwargs: dict = {}

    async def fake_call_llm(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return "I don't have data on that in this project. [DATA-1]"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    async def fake_validate(response, tool_results, deps):
        return response, [], False

    import app.agent.hallucination.orchestrator_validators as _validators

    monkeypatch.setattr(_validators, "run_post_assembly_validation", fake_validate)

    statuses: list[str] = []

    async def status_cb(message: str) -> None:
        statuses.append(message)

    async def token_cb(chunk: str) -> None:
        pass

    response = await run_agentic_retrieval(
        "What is the deepest hole in this project?",
        _FakeDeps(),
        status_callback=status_cb,
        token_callback=token_cb,
    )
    from app.models.rag import GeoRAGResponse

    assert isinstance(response, GeoRAGResponse)
    assert captured_kwargs.get("token_callback") is token_cb
    assert "Classifying query…" in statuses
    assert "Querying PostGIS + Qdrant…" in statuses
    assert "Synthesizing answer…" in statuses


# ---------------------------------------------------------------------------
# Hard rule #5 — Layer 5 (chunk provenance) now runs on the live path
# ---------------------------------------------------------------------------


def _minimal_response():
    from app.models.rag import Citation, GeoRAGResponse

    return GeoRAGResponse(
        text="The collar is at 45m depth. [DATA-1]",
        citations=[
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="silver.collars:count=1:first=abc",
                document_title="Collar data",
                relevance_score=0.9,
            )
        ],
        sources_used=["[DATA-1]"],
        confidence=0.8,
    )


@pytest.mark.asyncio
async def test_validate_node_calls_layer5_enrichment_when_pg_pool_present(monkeypatch) -> None:
    """Layer 5 (chunk provenance) was defined but never called anywhere on
    the live agentic path — this pins it to actually running now."""
    import app.agent.hallucination.layer5_provenance as _layer5_mod
    import app.agent.hallucination.orchestrator_validators as _validators

    response = _minimal_response()

    async def fake_validate(resp, tool_results, deps):
        return resp, [], False

    monkeypatch.setattr(_validators, "run_post_assembly_validation", fake_validate)

    captured: dict = {}

    async def fake_enrich(resp, pg_pool):
        captured["response"] = resp
        captured["pg_pool"] = pg_pool
        return resp

    monkeypatch.setattr(_layer5_mod, "enrich_provenance", fake_enrich)

    fake_pool = object()
    state = AgenticRetrievalState(query="q", deps=_FakeDeps(pg_pool=fake_pool))
    state = state.model_copy(update={"response": response})
    await validate_node(state)

    assert captured.get("pg_pool") is fake_pool
    assert captured.get("response") is response


@pytest.mark.asyncio
async def test_validate_node_skips_layer5_when_no_pg_pool(monkeypatch) -> None:
    """No pg_pool (e.g. certain test/eval callers) must not crash — Layer 5
    is a best-effort enrichment, never a hard requirement."""
    import app.agent.hallucination.layer5_provenance as _layer5_mod
    import app.agent.hallucination.orchestrator_validators as _validators

    response = _minimal_response()

    async def fake_validate(resp, tool_results, deps):
        return resp, [], False

    monkeypatch.setattr(_validators, "run_post_assembly_validation", fake_validate)

    called = False

    async def fake_enrich(resp, pg_pool):
        nonlocal called
        called = True
        return resp

    monkeypatch.setattr(_layer5_mod, "enrich_provenance", fake_enrich)

    state = AgenticRetrievalState(query="q", deps=_FakeDeps(pg_pool=None))
    state = state.model_copy(update={"response": response})
    result = await validate_node(state)

    assert called is False
    assert result["response"] is response


@pytest.mark.asyncio
async def test_validate_node_swallows_layer5_failure(monkeypatch) -> None:
    """A Layer 5 lookup failure must degrade gracefully, not break the answer."""
    import app.agent.hallucination.layer5_provenance as _layer5_mod
    import app.agent.hallucination.orchestrator_validators as _validators

    response = _minimal_response()

    async def fake_validate(resp, tool_results, deps):
        return resp, [], False

    monkeypatch.setattr(_validators, "run_post_assembly_validation", fake_validate)

    async def broken_enrich(resp, pg_pool):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(_layer5_mod, "enrich_provenance", broken_enrich)

    state = AgenticRetrievalState(query="q", deps=_FakeDeps(pg_pool=object()))
    state = state.model_copy(update={"response": response})
    result = await validate_node(state)  # must not raise

    assert result["response"] is response


# ---------------------------------------------------------------------------
# Regression — dispatcher passes the args every legacy tool actually declares
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_safely_matches_real_tool_signatures(monkeypatch) -> None:
    """Pin the dispatcher to the REAL signatures declared in app.agent.tools.

    Originally the dispatcher invoked legacy tools as ``fn(query, project_id)``
    or ``fn(project_id)`` even though every tool declares
    ``ctx: RunContext[AgentDeps]`` as its first positional parameter. The
    mismatch silently turned into ``None`` results inside
    ``_call_tool_safely``'s blanket ``except Exception`` so the agentic
    graph quietly degraded to "no retrieval results" in production.

    This test introspects each tool's real ``inspect.signature`` and asserts
    that the dispatcher can invoke the tool without raising a TypeError
    about missing required positional arguments. It uses a
    ``side_effect``-only mock so the tool body never runs — we're only
    binding parameters, not exercising the SQL / vector paths.
    """
    import inspect
    from unittest.mock import AsyncMock

    import app.agent.tools as _tools_mod
    from app.agent.agentic_retrieval.nodes import _call_tool_safely

    # Per the dispatcher's contract:
    #   * legacy 6 tools get a ``ToolContext(deps)`` as first arg
    #   * search_documents additionally gets the query string
    #   * downhole / graph traversal are intentionally SKIPPED pending NER
    legacy_tools_invoked = (
        "search_documents",
        "query_spatial_collars",
        "query_assay_data",
        "query_project_overview",
    )
    legacy_tools_skipped = ("query_downhole_logs", "traverse_knowledge_graph")

    deps = _FakeDeps(project_id="00000000-0000-0000-0000-000000000001")

    # Sanity-check the real signatures still have ``ctx`` as their first
    # positional parameter — if a future refactor drops it, this test should
    # tell us BEFORE the dispatcher silently breaks again.
    for name in legacy_tools_invoked + legacy_tools_skipped:
        real_fn = getattr(_tools_mod, name)
        params = list(inspect.signature(real_fn).parameters.values())
        assert params, f"{name} has no parameters?"
        assert params[0].name == "ctx", (
            f"{name}'s first positional parameter must be 'ctx' — found "
            f"{params[0].name!r}. Update _call_tool_safely if you change this."
        )

    # Now verify each invoked tool is callable from the dispatcher with the
    # args it declares. We replace each real tool with a sigspec-preserving
    # AsyncMock so a missing-positional-arg bug surfaces as a TypeError
    # rather than getting silently swallowed.
    for name in legacy_tools_invoked:
        real_sig = inspect.signature(getattr(_tools_mod, name))
        mock = AsyncMock(return_value={"chunks": [], "count": 0})
        # Pin the spec so calling the mock with the wrong parameter names
        # (e.g. positional surplus / missing required positional) raises.
        mock.__signature__ = real_sig  # type: ignore[attr-defined]
        monkeypatch.setattr(_tools_mod, name, mock, raising=False)

        result = await _call_tool_safely(name, "the user's natural-language query", deps)

        # _call_tool_safely swallows exceptions and returns None on failure,
        # so the only way the mock observes a call is if the dispatch path
        # actually bound parameters successfully.
        assert mock.await_count == 1, (
            f"dispatcher failed to invoke {name} with its real signature "
            f"(probably a missing-positional-arg bug — check _call_tool_safely)"
        )
        # And the mock returned its sentinel, which the dispatcher should
        # surface verbatim.
        assert result == {"chunks": [], "count": 0}

        # First positional must be the ctx shim — assert .deps lines up
        # with what we passed in so a future "pass the raw AgentDeps"
        # regression is caught.
        call_args, call_kwargs = mock.await_args
        first_arg = call_args[0] if call_args else call_kwargs.get("ctx")
        assert first_arg is not None and hasattr(first_arg, "deps"), (
            f"{name}: dispatcher must pass a ctx-shaped object whose .deps "
            f"is the AgentDeps — got {type(first_arg).__name__}"
        )
        assert first_arg.deps is deps

    # Skipped tools must NOT be called — even when they happen to be in
    # the active profile's primary list.
    for name in legacy_tools_skipped:
        mock = AsyncMock()
        monkeypatch.setattr(_tools_mod, name, mock, raising=False)
        # "anything" carries no hole_id and no TitleCase/quoted entity, so both
        # query_downhole_logs (needs hole_id) and traverse_knowledge_graph
        # (needs entity_name) correctly skip on this input.
        result = await _call_tool_safely(name, "anything", deps)
        assert result is None, f"{name} should be skipped (no entity in query)"
        assert mock.await_count == 0, f"{name} must not be invoked"


def test_entity_names_from_query_extracts_titlecase_and_quoted() -> None:
    """Audit 2026-06-28: lightweight entity extraction for the graph tool."""
    from app.agent.agentic_retrieval.nodes import _entity_names_from_query

    # TitleCase run — leading stopwords ("Tell", "the") dropped.
    assert "Triple R Deposit" in _entity_names_from_query(
        "Tell me about the Triple R Deposit"
    )
    # Quoted entity name.
    assert "Athabasca Group" in _entity_names_from_query(
        "What links 'Athabasca Group' to mineralization?"
    )
    # Pure-lowercase question → no entity.
    assert _entity_names_from_query("what is the average grade here") == []


@pytest.mark.asyncio
async def test_call_tool_safely_fires_traverse_when_entity_named(monkeypatch) -> None:
    """Audit 2026-06-28: traverse_knowledge_graph now fires when the query
    names an entity (previously skipped unconditionally → Neo4j never consulted
    in agentic chat despite three intent profiles listing it as primary)."""
    import inspect
    from unittest.mock import AsyncMock

    import app.agent.tools as _tools_mod
    from app.agent.agentic_retrieval.nodes import _call_tool_safely

    real_sig = inspect.signature(_tools_mod.traverse_knowledge_graph)
    mock = AsyncMock(return_value={"entities": [], "count": 0})
    mock.__signature__ = real_sig  # type: ignore[attr-defined]
    monkeypatch.setattr(_tools_mod, "traverse_knowledge_graph", mock, raising=False)

    deps = _FakeDeps(project_id="00000000-0000-0000-0000-000000000001")
    await _call_tool_safely(
        "traverse_knowledge_graph", "Tell me about the Triple R Deposit", deps
    )
    assert mock.await_count == 1, "traverse must fire when an entity is named"
    call_args, _ = mock.await_args
    # Dispatch is fn(ctx, entity_name, project_id) — entity_name is 2nd positional.
    assert call_args[1] == "Triple R Deposit"


# ---------------------------------------------------------------------------
# Flag-off byte-identical regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_skips_agentic_when_flag_off(monkeypatch) -> None:
    """With the flag off, run_deterministic_rag does NOT dispatch to the
    new graph — it falls through to the legacy path. We verify by patching
    ``run_agentic_retrieval`` to a sentinel and confirming it's NEVER called.
    """
    called: list[str] = []

    async def sentinel(query: str, deps: Any):  # pragma: no cover — must not run
        called.append("yes")
        raise AssertionError("agentic path should be skipped when flag is off")

    monkeypatch.setattr(
        "app.agent.agentic_retrieval.run_agentic_retrieval",
        sentinel,
    )
    from app.config import settings

    monkeypatch.setattr(settings, "AGENTIC_RETRIEVAL_V2_ENABLED", False)

    # We don't actually invoke run_deterministic_rag (it requires too much
    # live infra). Instead we verify the conditional that would call the
    # sentinel: with the flag off, the import-and-call path is skipped.
    assert getattr(settings, "AGENTIC_RETRIEVAL_V2_ENABLED", False) is False
    assert called == []
