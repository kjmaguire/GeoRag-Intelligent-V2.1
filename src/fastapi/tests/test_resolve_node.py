"""Tests for plan §3e — multi-turn resolve_node wire."""

from __future__ import annotations

import pytest

from app.agent.agentic_retrieval.nodes import resolve_node
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.multi_turn_resolver import (
    ConversationTurn,
    EntityMention,
)
from app.config import settings as _settings


class _FakeDeps:
    project_id = "p"
    workspace_id = "ws-1"
    pg_pool = None


def _state(**overrides) -> AgenticRetrievalState:
    base = AgenticRetrievalState(query="x", deps=_FakeDeps())
    return base.model_copy(update=overrides)


def _turn_with_hole(idx: int, hole_id: str) -> ConversationTurn:
    return ConversationTurn(
        turn_index=idx,
        role="user",
        text=f"tell me about hole {hole_id}",
        entity_mentions=(
            EntityMention(
                surface_form=hole_id, entity_type="hole", turn_index=idx,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Flag-off default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_node_is_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(_settings, "MULTI_TURN_RESOLUTION_ENABLED", False, raising=False)
    state = _state(
        query="what are ITS top assays?",
        history=[_turn_with_hole(0, "PLS-22-08")],
    )
    update = await resolve_node(state)
    assert update == {}


# ---------------------------------------------------------------------------
# Empty history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_node_is_noop_when_history_empty(monkeypatch):
    monkeypatch.setattr(_settings, "MULTI_TURN_RESOLUTION_ENABLED", True, raising=False)
    state = _state(query="what are ITS top assays?", history=[])
    update = await resolve_node(state)
    assert update == {}


# ---------------------------------------------------------------------------
# Pronoun resolution rewrites the query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_node_rewrites_query_when_pronoun_resolves(monkeypatch):
    monkeypatch.setattr(_settings, "MULTI_TURN_RESOLUTION_ENABLED", True, raising=False)
    state = _state(
        query="what are ITS top assays?",
        history=[_turn_with_hole(0, "PLS-22-08")],
    )
    update = await resolve_node(state)
    assert update["query"] != "what are ITS top assays?"
    assert "PLS-22-08" in update["query"]
    # Original preserved.
    assert update["query_original"] == "what are ITS top assays?"
    # Trace populated.
    assert len(update["resolution_trace"]) == 1
    step = update["resolution_trace"][0]
    assert step["kind"] == "pronoun"
    assert step["resolved_to"] == "PLS-22-08"
    assert step["source_turn_index"] == 0
    # Confidence stamped (possessive pronoun = 0.85 per resolver).
    assert update["resolution_confidence"] > 0.0


# ---------------------------------------------------------------------------
# No-change path with flag on — confidence still stamps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_node_stamps_confidence_when_no_change(monkeypatch):
    """A pristine query (no references) gets confidence=1.0 even
    without a rewrite — it's a positive signal worth recording."""
    monkeypatch.setattr(_settings, "MULTI_TURN_RESOLUTION_ENABLED", True, raising=False)
    state = _state(
        # Neutral query — no pronouns, no demonstratives, no comparatives.
        query="what is the deepest drillhole in the corridor?",
        history=[_turn_with_hole(0, "PLS-22-08")],
    )
    update = await resolve_node(state)
    # No rewrite — but confidence stamped.
    assert "query" not in update or update.get("query") == state.query
    assert update["resolution_confidence"] == 1.0


# ---------------------------------------------------------------------------
# Defensive: exception doesn't block answer path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_node_swallows_exceptions(monkeypatch):
    """A bug inside resolve_multi_turn shouldn't crash the graph."""
    monkeypatch.setattr(_settings, "MULTI_TURN_RESOLUTION_ENABLED", True, raising=False)

    # Force resolve_multi_turn to raise.
    import app.agent.multi_turn_resolver as _mod

    def _explode(query, history):
        raise RuntimeError("simulated resolver bug")

    monkeypatch.setattr(_mod, "resolve_multi_turn", _explode, raising=False)

    state = _state(
        query="what are ITS top assays?",
        history=[_turn_with_hole(0, "PLS-22-08")],
    )
    # Must not raise; returns empty dict (= no-op fallback).
    update = await resolve_node(state)
    assert update == {}


# ---------------------------------------------------------------------------
# Graph pipeline shape
# ---------------------------------------------------------------------------


def test_resolve_node_runs_first_in_pipeline():
    """Locks the pipeline shape: resolve → classify → route → ..."""
    from app.agent.agentic_retrieval.graph import _PIPELINE

    names = [name for name, _ in _PIPELINE]
    assert names[0] == "resolve"
    assert names[1] == "classify"


# ---------------------------------------------------------------------------
# State carries the four new resolution fields
# ---------------------------------------------------------------------------


def test_state_carries_resolution_fields():
    state = AgenticRetrievalState(query="q", deps=_FakeDeps())
    assert state.history == []
    assert state.query_original is None
    assert state.resolution_trace == []
    assert state.resolution_confidence is None


# ---------------------------------------------------------------------------
# Flag default
# ---------------------------------------------------------------------------


def test_multi_turn_flag_defaults_to_false():
    from app.config import Settings  # noqa: PLC0415

    assert Settings.model_fields["MULTI_TURN_RESOLUTION_ENABLED"].default is False


# ---------------------------------------------------------------------------
# Entry-point history threading
# ---------------------------------------------------------------------------


def test_run_agentic_retrieval_accepts_history_kwarg():
    """Signature lock — adding/removing the history kwarg is a breaking
    change for the Laravel-side bridge once the loader lands."""
    import inspect

    from app.agent.agentic_retrieval.graph import run_agentic_retrieval

    sig = inspect.signature(run_agentic_retrieval)
    assert "history" in sig.parameters
    assert sig.parameters["history"].default is None
