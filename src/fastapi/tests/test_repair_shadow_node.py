"""Tests for plan §4b/§4c Stage 1 — repair-loop shadow-mode wire."""

from __future__ import annotations

import pytest

from app.agent.agentic_retrieval.nodes import repair_shadow_node
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.guards import GuardErrorCode
from app.agent.repair_strategy import RepairStrategy
from app.config import settings as _settings

# ---------------------------------------------------------------------------
# Minimal AgentDeps stand-in (avoids importing the real one which pulls
# asyncpg etc. — the shadow node never touches deps).
# ---------------------------------------------------------------------------


class _FakeDeps:
    project_id = "test-project"
    workspace_id = "ws-1"
    pg_pool = None
    openai_http_client = None
    anthropic_client = None


def _state(**overrides) -> AgenticRetrievalState:
    base = AgenticRetrievalState(
        query="q",
        deps=_FakeDeps(),
        intent="synthesis",
        effective_intent="synthesis",
        tool_results=[("search_documents", {"chunks": ["x"]})],
        validation_warnings=[],
        demotion_reasons=[],
    )
    return base.model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Flag default: no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_node_is_noop_when_flag_off(monkeypatch):
    """REPAIR_LOOP_SHADOW_ENABLED=False (default) → empty update dict,
    state unchanged."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", False, raising=False)
    state = _state(validation_warnings=["layer 3: ungrounded number 5.0"])
    update = await repair_shadow_node(state)
    assert update == {}


# ---------------------------------------------------------------------------
# Flag on, no guards firing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_node_with_clean_state_produces_empty_strategies(monkeypatch):
    """Flag on, no warnings + tool_results populated + citations present →
    classify_guards returns empty → plan_repair returns empty plan."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)

    # Build a minimal GeoRAGResponse with a non-empty citations list so
    # CITATION_INCOMPLETE doesn't fire.
    from app.models.rag import Citation, GeoRAGResponse  # noqa: PLC0415

    response = GeoRAGResponse(
        text="answer",
        citations=[
            Citation(
                citation_id="[DATA:1]",
                source_chunk_id="00000000-0000-0000-0000-000000000001",
                document_title="Test Doc",
                relevance_score=0.9,
                citation_type="DATA",
            ),
        ],
        confidence=0.85,
        sources_used=["00000000-0000-0000-0000-000000000001"],
    )
    state = _state(response=response, validation_warnings=[], demotion_reasons=[])
    update = await repair_shadow_node(state)
    # No codes fired → empty strategies + terminal=False from plan_repair
    # → reason is None.
    assert update["repair_codes_observed"] == []
    assert update["repair_strategy_history"] == []
    assert update["repair_terminal_reason"] is None


# ---------------------------------------------------------------------------
# Flag on, a guard fires → loop-friendly plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_node_emits_loop_friendly_strategies_for_layer3_warning(
    monkeypatch,
):
    """A 'layer 3: ungrounded number' warning maps to
    NUMERIC_GROUNDING_FAILED → REPHRASE_NUMERIC_CLAIM. Not terminal."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)

    from app.models.rag import Citation, GeoRAGResponse  # noqa: PLC0415

    response = GeoRAGResponse(
        text="answer with 5.0 g/t",
        citations=[
            Citation(
                citation_id="[DATA:1]",
                source_chunk_id="00000000-0000-0000-0000-000000000001",
                document_title="Test Doc",
                relevance_score=0.9,
                citation_type="DATA",
            ),
        ],
        confidence=0.7,
        sources_used=["00000000-0000-0000-0000-000000000001"],
    )
    state = _state(
        response=response,
        validation_warnings=["layer 3: ungrounded number 5.0"],
        demotion_reasons=[],
    )
    update = await repair_shadow_node(state)
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED.value in update["repair_codes_observed"]
    assert (
        RepairStrategy.REPHRASE_NUMERIC_CLAIM.value
        in update["repair_strategy_history"]
    )
    # Loop-friendly path → terminal_reason is None.
    assert update["repair_terminal_reason"] is None


# ---------------------------------------------------------------------------
# Terminal path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_node_emits_terminal_reason_for_conflict(monkeypatch):
    """conflicting_evidence present → CONFLICTING_SOURCES → SURFACE_CONFLICT
    (terminal). Plan's reason gets stamped onto state."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)

    from app.models.rag import Citation, GeoRAGResponse  # noqa: PLC0415

    response = GeoRAGResponse(
        text="answer",
        citations=[
            Citation(
                citation_id="[DATA:1]",
                source_chunk_id="00000000-0000-0000-0000-000000000001",
                document_title="Test Doc",
                relevance_score=0.9,
                citation_type="DATA",
            ),
        ],
        confidence=0.6,
        sources_used=["00000000-0000-0000-0000-000000000001"],
        conflicting_evidence=[
            {
                "entity_key": "depth",
                "property_name": "total_depth_m",
                "evidence_ids": ["c1", "c2"],
                "values": [125.0, 130.0],
            },
        ],
    )
    state = _state(response=response)
    update = await repair_shadow_node(state)
    assert GuardErrorCode.CONFLICTING_SOURCES.value in update["repair_codes_observed"]
    assert (
        RepairStrategy.SURFACE_CONFLICT.value
        in update["repair_strategy_history"]
    )
    # Terminal path → reason is populated and mentions SURFACE_CONFLICT.
    assert update["repair_terminal_reason"] is not None
    assert "SURFACE_CONFLICT" in update["repair_terminal_reason"]


# ---------------------------------------------------------------------------
# Non-mutation guarantee
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_node_does_not_mutate_response(monkeypatch):
    """Even with guards firing, shadow mode MUST NOT touch state.response,
    state.tool_results, state.retrieval_profile, or state.retrieval_filters."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)

    from app.models.rag import Citation, GeoRAGResponse  # noqa: PLC0415

    response = GeoRAGResponse(
        text="answer",
        citations=[
            Citation(
                citation_id="[DATA:1]",
                source_chunk_id="00000000-0000-0000-0000-000000000001",
                document_title="Test Doc",
                relevance_score=0.9,
                citation_type="DATA",
            ),
        ],
        confidence=0.5,
        sources_used=["00000000-0000-0000-0000-000000000001"],
    )
    state = _state(
        response=response,
        validation_warnings=["layer 3: ungrounded number"],
    )
    # Snapshot before.
    before_response_text = state.response.text
    before_tool_results = list(state.tool_results)

    update = await repair_shadow_node(state)

    # The shadow node MUST NOT include 'response' or 'tool_results' in its
    # update dict — LangGraph would merge them into state otherwise.
    assert "response" not in update
    assert "tool_results" not in update
    assert "retrieval_profile" not in update
    assert "retrieval_filters" not in update
    # State on disk is unchanged.
    assert state.response.text == before_response_text
    assert state.tool_results == before_tool_results


# ---------------------------------------------------------------------------
# Defensive: shadow path doesn't break on missing response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_node_handles_missing_response_gracefully(monkeypatch):
    """state.response is None in tests that drive nodes directly;
    shadow MUST NOT crash on that path."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)

    state = _state(response=None)
    update = await repair_shadow_node(state)
    # Returns a dict (possibly with codes/strategies if guard-from-tools
    # fired); shouldn't raise.
    assert isinstance(update, dict)


# ---------------------------------------------------------------------------
# Field defaults on state
# ---------------------------------------------------------------------------


def test_state_carries_repair_loop_fields():
    """Regression — the four shadow-loop state fields must be present
    with their default values when AgenticRetrievalState is constructed."""
    state = AgenticRetrievalState(query="q", deps=_FakeDeps())
    assert state.repair_attempts == []
    assert state.repair_strategy_history == []
    assert state.repair_terminal_reason is None
    assert state.repair_codes_observed == []


# ---------------------------------------------------------------------------
# Graph wiring — shadow node is in the pipeline
# ---------------------------------------------------------------------------


def test_graph_pipeline_includes_repair_shadow_between_demote_and_persist():
    """Locks the pipeline shape: repair_shadow sits AFTER demote and
    BEFORE persist."""
    from app.agent.agentic_retrieval.graph import _PIPELINE

    names = [name for name, _ in _PIPELINE]
    assert "repair_shadow" in names
    # Ordering: demote → repair_shadow → persist
    demote_idx = names.index("demote")
    shadow_idx = names.index("repair_shadow")
    persist_idx = names.index("persist")
    assert demote_idx < shadow_idx < persist_idx


# ---------------------------------------------------------------------------
# Config flag default
# ---------------------------------------------------------------------------


def test_repair_loop_shadow_flag_defaults_to_false():
    """Production safety — the shadow flag MUST default to off so a
    fresh deploy doesn't silently change trace shapes."""
    # Read the class-level default rather than the instance to avoid
    # reading a test-mutated value.
    from app.config import Settings  # noqa: PLC0415

    assert Settings.model_fields["REPAIR_LOOP_SHADOW_ENABLED"].default is False
