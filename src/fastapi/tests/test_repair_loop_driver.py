"""Tests for plan §4b Stages 3 + 4 loop driver — the orchestrator
that calls plan_repair → apply_*_strategy → re-issue iteratively."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.agentic_retrieval.nodes import (
    _run_repair_loop,
    _snapshot_field,
    repair_shadow_node,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.repair_strategy import RepairPlan, RepairStrategy
from app.config import settings as _settings


class _FakeDeps:
    project_id = "p"
    workspace_id = "ws-1"
    pg_pool = None
    openai_http_client = None
    anthropic_client = None


def _make_response(text: str = "stub", with_citation: bool = True):
    """Build a minimal GeoRAGResponse for state.response."""
    from app.models.rag import Citation, GeoRAGResponse  # noqa: PLC0415

    citations = []
    if with_citation:
        citations.append(
            Citation(
                citation_id="[DATA:1]",
                source_chunk_id="00000000-0000-0000-0000-000000000001",
                document_title="T",
                relevance_score=0.9,
                citation_type="DATA",
            ),
        )
    return GeoRAGResponse(
        text=text,
        citations=citations,
        confidence=0.7,
        sources_used=["00000000-0000-0000-0000-000000000001"],
    )


def _state_with_response(**overrides) -> AgenticRetrievalState:
    base = AgenticRetrievalState(
        query="q",
        deps=_FakeDeps(),
        intent="synthesis",
        effective_intent="synthesis",
        tool_results=[("search_documents", {"chunks": ["x"]})],
        response=_make_response(),
    )
    return base.model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Flags off → loop is a complete no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_driver_is_noop_when_both_flags_off(monkeypatch):
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_FULL_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_TERMINAL_ENABLED", False, raising=False)

    state = _state_with_response(
        validation_warnings=["layer 3: ungrounded number 5.0"],
    )
    update = await repair_shadow_node(state)

    # Shadow telemetry stamps codes + strategies — but NO loop fields.
    assert "repair_codes_observed" in update
    assert "repair_strategy_history" in update
    # No retrieval_filters / tool_results / response mutations.
    assert "tool_results" not in update
    assert "retrieval_filters" not in update
    # repair_attempts stays empty.
    assert update.get("repair_attempts") in (None, [], )


# ---------------------------------------------------------------------------
# Stage 3 — LLM-only retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage3_lowcost_reissues_llm_on_numeric_grounding(monkeypatch):
    """REPAIR_LOOP_LOWCOST_ENABLED + NUMERIC_GROUNDING_FAILED → the
    driver applies REPHRASE_NUMERIC_CLAIM, re-calls _call_llm with
    the suffix, and updates state.response."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_FULL_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 2, raising=False)

    captured_prompts: list[str] = []

    async def fake_call_llm(*args, **kwargs):
        captured_prompts.append(kwargs.get("system_prompt", ""))
        return "rephrased answer with ESTIMATED markers [DATA:1]"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    state = _state_with_response(
        validation_warnings=["layer 3: ungrounded number 5.0"],
    )

    update = await repair_shadow_node(state)

    # The driver attempted Stage 3 — should have called LLM at least once
    # with the REPHRASE suffix appended.
    assert any("ESTIMATED" in p for p in captured_prompts), (
        f"expected REPHRASE suffix in prompt; got {captured_prompts}"
    )
    # state.response.text reflects the rephrased answer.
    assert "rephrased" in state.response.text
    # repair_strategy_history records REPHRASE.
    assert "REPHRASE_NUMERIC_CLAIM" in update.get("repair_strategy_history", [])


@pytest.mark.asyncio
async def test_stage3_swallows_llm_failure(monkeypatch):
    """A failing LLM call inside the loop must NOT crash repair_shadow_node."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", True, raising=False)

    async def broken_call_llm(*args, **kwargs):
        raise RuntimeError("simulated LLM failure")

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", broken_call_llm)

    state = _state_with_response(
        validation_warnings=["layer 3: ungrounded number 5.0"],
    )

    # Must not raise.
    update = await repair_shadow_node(state)
    assert isinstance(update, dict)


# ---------------------------------------------------------------------------
# Stage 4 — retrieval re-issue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage4_full_reissues_execute_and_assemble_on_over_filtered(monkeypatch):
    """REPAIR_LOOP_FULL_ENABLED + OVER_FILTERED_QUERY → driver applies
    LOOSEN_FILTERS, re-runs execute_node + assemble_node."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_FULL_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 2, raising=False)

    from app.agent.agentic_retrieval import preprocessor as _pp_mod
    from app.agent.agentic_retrieval import retrieval_profile as _rp_mod
    from app.agent.agentic_retrieval import nodes as _nodes_mod

    # Build minimal RetrievalProfile + RetrievalFilters so apply_*
    # has something concrete to mutate.
    profile = _rp_mod.profile_for_intent("synthesis")
    filters = _pp_mod.preprocess_envelope(None)

    state = _state_with_response(
        validation_warnings=["over-filtered query — relaxing filter set"],
        retrieval_profile=profile,
        retrieval_filters=filters,
    )

    execute_calls = 0
    assemble_calls = 0

    async def fake_execute(s):
        nonlocal execute_calls
        execute_calls += 1
        return {"tool_results": [("search_documents", {"chunks": ["new"]})], "evidence_packet": None}

    async def fake_assemble(s):
        nonlocal assemble_calls
        assemble_calls += 1
        s.response = _make_response("repaired answer [DATA:1]")
        return {"response": s.response}

    monkeypatch.setattr(_nodes_mod, "execute_node", fake_execute)
    monkeypatch.setattr(_nodes_mod, "assemble_node", fake_assemble)

    update = await repair_shadow_node(state)

    assert execute_calls >= 1, "Stage 4 should re-run execute_node"
    assert assemble_calls >= 1, "Stage 4 should re-run assemble_node"
    assert "LOOSEN_FILTERS" in update.get("repair_strategy_history", [])


# ---------------------------------------------------------------------------
# Max attempts cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_caps_at_max_attempts(monkeypatch):
    """The driver MUST NOT loop past REPAIR_LOOP_MAX_ATTEMPTS."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 1, raising=False)

    llm_call_count = 0

    async def fake_call_llm(*args, **kwargs):
        nonlocal llm_call_count
        llm_call_count += 1
        # Return text with ANOTHER ungrounded number to keep the
        # validation warning live — would loop forever without the cap.
        return "still has ungrounded 6.0 [DATA:1]"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    state = _state_with_response(
        validation_warnings=["layer 3: ungrounded number 5.0"],
    )

    await repair_shadow_node(state)

    # With MAX_ATTEMPTS=1, the loop fires exactly once.
    assert llm_call_count == 1


@pytest.mark.asyncio
async def test_max_attempts_zero_disables_loop(monkeypatch):
    """REPAIR_LOOP_MAX_ATTEMPTS=0 disables the loop entirely
    (even with flags on)."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 0, raising=False)

    llm_call_count = 0

    async def fake_call_llm(*args, **kwargs):
        nonlocal llm_call_count
        llm_call_count += 1
        return "repaired"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    state = _state_with_response(
        validation_warnings=["layer 3: ungrounded number 5.0"],
    )

    await repair_shadow_node(state)
    assert llm_call_count == 0


# ---------------------------------------------------------------------------
# Terminal short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_plan_skips_loop_driver(monkeypatch):
    """When the plan is terminal (e.g. CONFLICTING_SOURCES), the loop
    driver MUST NOT fire — Stage 2 stamping handles it instead."""
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_FULL_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_TERMINAL_ENABLED", True, raising=False)

    llm_call_count = 0

    async def fake_call_llm(*args, **kwargs):
        nonlocal llm_call_count
        llm_call_count += 1
        return "x"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    response = _make_response()
    response.conflicting_evidence = [
        {
            "entity_key": "depth",
            "property_name": "total_depth_m",
            "evidence_ids": ["c1", "c2"],
            "values": [125.0, 130.0],
        },
    ]

    state = _state_with_response(response=response)
    await repair_shadow_node(state)
    # Terminal SURFACE_CONFLICT — no LLM retry should have fired.
    assert llm_call_count == 0


# ---------------------------------------------------------------------------
# _snapshot_field
# ---------------------------------------------------------------------------


def test_snapshot_field_handles_none():
    assert _snapshot_field(None) == {}


def test_snapshot_field_handles_pydantic_model():
    from pydantic import BaseModel  # noqa: PLC0415

    class _Sample(BaseModel):
        a: int = 1
        b: str = "x"

    snap = _snapshot_field(_Sample())
    assert snap == {"a": 1, "b": "x"}


def test_snapshot_field_handles_plain_object():
    class _Obj:
        def __init__(self):
            self.foo = "bar"

    snap = _snapshot_field(_Obj())
    assert snap == {"foo": "bar"}
