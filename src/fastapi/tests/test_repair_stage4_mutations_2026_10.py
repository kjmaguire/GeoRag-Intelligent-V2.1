"""Audit 2026-10 finding 20: Stage 4 repair mutations reach a frozen dataclass.

``_reissue_retrieval`` applied the strategy's field updates with
``state.retrieval_filters.model_copy(update=...)``. ``RetrievalFilters`` is a
FROZEN DATACLASS (preprocessor.py), not a Pydantic model: it has no
``model_copy``, the AttributeError was swallowed by a ``logger.debug``, and every
filter mutation (LOOSEN_FILTERS above all) silently did nothing while the loop
paid for a second retrieval and LLM call with the filters it already had. The
three ``REPAIR_LOOP_*`` flags that reach this code default to False.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.agent.agentic_retrieval import nodes as nodes_mod
from app.agent.agentic_retrieval import preprocessor as pp_mod
from app.agent.agentic_retrieval import retrieval_profile as rp_mod
from app.agent.agentic_retrieval.nodes import (
    _apply_state_mutation,
    _reissue_retrieval,
    _snapshot_field,
    repair_shadow_node,
)
from app.agent.agentic_retrieval.preprocessor import RetrievalFilters
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.repair_apply import apply_retrieval_strategy
from app.agent.repair_strategy import RepairStrategy
from app.config import settings as _settings
from app.models.rag import Citation, GeoRAGResponse


class _Deps:
    project_id = "p"
    workspace_id = "ws-1"
    pg_pool = None
    openai_http_client = None
    anthropic_client = None


def _response(text: str = "stub") -> GeoRAGResponse:
    citation = Citation(
        citation_id="[DATA:1]",
        source_chunk_id="00000000-0000-0000-0000-000000000001",
        document_title="T",
        relevance_score=0.9,
        citation_type="DATA",
    )
    return GeoRAGResponse(
        text=text,
        citations=[citation],
        confidence=0.7,
        sources_used=["00000000-0000-0000-0000-000000000001"],
    )


def _state(filters: RetrievalFilters | None, **overrides: Any) -> AgenticRetrievalState:
    base = AgenticRetrievalState(
        query="q",
        deps=_Deps(),
        intent="synthesis",
        effective_intent="synthesis",
        tool_results=[("search_documents", {"chunks": ["x"]})],
        response=_response(),
        retrieval_profile=rp_mod.profile_for_intent("synthesis"),
        retrieval_filters=filters,
    )
    return base.model_copy(update=overrides)


def _narrowed() -> RetrievalFilters:
    return RetrievalFilters(
        allowed_data_sources=frozenset({"drill_logs", "assays"}),
        reporting_code="JORC",
        reporting_code_was_defaulted=False,
        prompt_suffixes=("be brief",),
        specific_objects=("PLS-22-08",),
    )


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[RetrievalFilters | None]:
    """Replace execute / assemble; record the filters execute_node ran with."""
    filters_seen: list[RetrievalFilters | None] = []

    async def fake_execute(s: AgenticRetrievalState) -> dict[str, Any]:
        filters_seen.append(s.retrieval_filters)
        return {"tool_results": [("search_documents", {"chunks": ["new"]})], "evidence_packet": None}

    async def fake_assemble(s: AgenticRetrievalState) -> dict[str, Any]:
        return {"response": _response("repaired answer [DATA:1]")}

    monkeypatch.setattr(nodes_mod, "execute_node", fake_execute)
    monkeypatch.setattr(nodes_mod, "assemble_node", fake_assemble)
    return filters_seen


# ---------------------------------------------------------------------------
# _reissue_retrieval
# ---------------------------------------------------------------------------


async def test_loosening_the_filters_changes_the_filters_the_retry_runs_with(
    seen: list[RetrievalFilters | None],
) -> None:
    state = _state(_narrowed())
    mutations = apply_retrieval_strategy(
        RepairStrategy.LOOSEN_FILTERS,
        {
            "retrieval_profile": _snapshot_field(state.retrieval_profile),
            "retrieval_filters": _snapshot_field(state.retrieval_filters),
        },
    )
    assert mutations["retrieval_filters"]["allowed_data_sources"] == []  # what the strategy asks for

    await _reissue_retrieval(state, mutations)

    assert seen, "execute_node never ran"
    ran_with = seen[0]
    assert ran_with is not None
    assert ran_with.allowed_data_sources == frozenset()
    assert isinstance(ran_with.allowed_data_sources, frozenset)  # not the strategy's list
    assert state.retrieval_filters is not None
    assert state.retrieval_filters.allowed_data_sources == frozenset()
    # Everything the strategy did not touch is untouched.
    assert state.retrieval_filters.reporting_code == "JORC"
    assert state.retrieval_filters.reporting_code_was_defaulted is False
    assert state.retrieval_filters.prompt_suffixes == ("be brief",)
    assert state.retrieval_filters.specific_objects == ("PLS-22-08",)


async def test_the_retry_replaces_the_answer_and_the_evidence(
    seen: list[RetrievalFilters | None],
) -> None:
    state = _state(_narrowed())

    await _reissue_retrieval(state, {"retrieval_filters": {"max_chunks": 40}})

    assert state.retrieval_filters is not None and state.retrieval_filters.max_chunks == 40
    assert state.response is not None and state.response.text == "repaired answer [DATA:1]"
    assert state.tool_results == [("search_documents", {"chunks": ["new"]})]


async def test_a_pydantic_profile_is_still_updated(seen: list[RetrievalFilters | None]) -> None:
    state = _state(_narrowed())
    assert not state.retrieval_profile.adversarial_pass_enabled

    await _reissue_retrieval(state, {"retrieval_profile": {"adversarial_pass_enabled": True}})

    assert state.retrieval_profile.adversarial_pass_enabled is True
    assert state.retrieval_profile.intent == "synthesis"


async def test_a_profile_field_that_does_not_exist_is_ignored_and_said_so(
    seen: list[RetrievalFilters | None], caplog: pytest.LogCaptureFixture
) -> None:
    # BROADEN_KNN / INCREASE_GRAPH_DEPTH emit candidate_count_pre_rerank /
    # graph_max_hops; RetrievalProfile declares neither.
    state = _state(_narrowed())

    with caplog.at_level(logging.WARNING, logger=nodes_mod.logger.name):
        await _reissue_retrieval(
            state,
            {"retrieval_profile": {"adversarial_pass_enabled": True, "candidate_count_pre_rerank": 80}},
        )

    assert state.retrieval_profile.adversarial_pass_enabled is True
    assert "candidate_count_pre_rerank" not in state.retrieval_profile.__dict__
    assert "candidate_count_pre_rerank" in " ".join(r.getMessage() for r in caplog.records)


async def test_with_no_filters_on_the_state_only_the_profile_is_touched(
    seen: list[RetrievalFilters | None],
) -> None:
    state = _state(None)

    await _reissue_retrieval(
        state,
        {
            "retrieval_filters": {"max_chunks": 40},
            "retrieval_profile": {"adversarial_pass_enabled": True},
        },
    )

    assert state.retrieval_filters is None
    assert state.retrieval_profile.adversarial_pass_enabled is True


# ---------------------------------------------------------------------------
# _apply_state_mutation
# ---------------------------------------------------------------------------


def test_the_dataclass_is_replaced_not_mutated() -> None:
    before = _narrowed()

    after = _apply_state_mutation(before, {"max_chunks": 7}, field="retrieval_filters")

    assert after is not before
    assert after.max_chunks == 7
    assert before.max_chunks is None  # frozen original untouched


def test_declared_container_types_survive_a_list_from_the_strategy() -> None:
    after = _apply_state_mutation(
        _narrowed(),
        {"allowed_data_sources": ["maps"], "prompt_suffixes": ["a", "b"]},
        field="retrieval_filters",
    )

    assert after.allowed_data_sources == frozenset({"maps"})
    assert after.prompt_suffixes == ("a", "b")


def test_a_field_the_dataclass_lacks_is_ignored_and_the_rest_still_applies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ENABLE_FUZZY_ENTITY / ADD_SPATIAL_BUFFER / TRANSFORM_CRS emit fields that
    # RetrievalFilters does not (yet) declare.
    with caplog.at_level(logging.WARNING, logger=nodes_mod.logger.name):
        after = _apply_state_mutation(
            _narrowed(),
            {"max_chunks": 9, "fuzzy_entity_matching": True, "spatial_buffer_m": 500.0},
            field="retrieval_filters",
        )

    assert after.max_chunks == 9
    assert not hasattr(after, "fuzzy_entity_matching")
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "fuzzy_entity_matching" in warned and "spatial_buffer_m" in warned


def test_a_value_the_dataclass_rejects_raises_instead_of_being_swallowed() -> None:
    # replace() re-runs __init__: an init=False field cannot be replaced. The
    # point is only that failures reach the caller, whose checkpoint restore
    # then keeps the validated answer.
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class _Locked:
        derived: int = field(init=False, default=1)

    with pytest.raises(TypeError, match="init=False"):
        _apply_state_mutation(_Locked(), {"derived": 2}, field="retrieval_filters")


# ---------------------------------------------------------------------------
# Through the repair loop
# ---------------------------------------------------------------------------


async def test_the_full_loop_applies_loosen_filters_on_an_over_filtered_query(
    monkeypatch: pytest.MonkeyPatch, seen: list[RetrievalFilters | None]
) -> None:
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_FULL_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 1, raising=False)
    state = _state(
        _narrowed(), validation_warnings=["over-filtered query — relaxing filter set"],
    )

    update = await repair_shadow_node(state)

    assert "LOOSEN_FILTERS" in update.get("repair_strategy_history", [])
    assert seen and seen[0] is not None
    assert seen[0].allowed_data_sources == frozenset(), (
        "the retry ran with the same narrowing the first attempt had"
    )


def test_the_default_preprocessed_filters_accept_a_round_trip() -> None:
    filters = pp_mod.preprocess_envelope(None)

    same = _apply_state_mutation(filters, _snapshot_field(filters), field="retrieval_filters")

    assert same == filters
