"""Unit tests for plan §4b Stages 3 + 4 strategy appliers."""

from __future__ import annotations

import pytest

from app.agent.repair_apply import (
    LLM_STRATEGY_SUFFIXES,
    apply_llm_only_strategy,
    apply_retrieval_strategy,
)
from app.agent.repair_strategy import RepairStrategy

# ---------------------------------------------------------------------------
# Stage 3 — LLM-only suffixes
# ---------------------------------------------------------------------------


def test_rephrase_numeric_returns_estimated_marker_instruction():
    suffix = apply_llm_only_strategy(RepairStrategy.REPHRASE_NUMERIC_CLAIM)
    assert suffix is not None
    assert "ESTIMATED" in suffix
    assert "REPAIR INSTRUCTION" in suffix


def test_citation_retry_returns_per_claim_citation_instruction():
    suffix = apply_llm_only_strategy(RepairStrategy.REQUEST_CITATION_RETRY)
    assert suffix is not None
    assert "[DATA:" in suffix


def test_non_llm_strategy_returns_none():
    """Stage 4 + terminal strategies are NOT Stage-3-eligible."""
    for strategy in (
        RepairStrategy.LOOSEN_FILTERS,
        RepairStrategy.BROADEN_KNN,
        RepairStrategy.SURFACE_CONFLICT,
        RepairStrategy.ASK_FOR_DISAMBIGUATION,
    ):
        assert apply_llm_only_strategy(strategy) is None


def test_llm_strategy_suffixes_table_covers_two_strategies():
    """Lock the Stage 3 set."""
    assert set(LLM_STRATEGY_SUFFIXES.keys()) == {
        "REPHRASE_NUMERIC_CLAIM",
        "REQUEST_CITATION_RETRY",
    }


# ---------------------------------------------------------------------------
# Stage 4 — Retrieval-side
# ---------------------------------------------------------------------------


def test_loosen_filters_clears_year_and_data_sources():
    result = apply_retrieval_strategy(
        RepairStrategy.LOOSEN_FILTERS,
        {
            "retrieval_filters": {
                "from_year": 1985,
                "to_year": 2020,
                "year_range_strict": True,
                "allowed_data_sources": ["NI 43-101", "Annual Report"],
            },
        },
    )
    nf = result["retrieval_filters"]
    assert nf["from_year"] is None
    assert nf["to_year"] is None
    assert nf["year_range_strict"] is None
    assert nf["allowed_data_sources"] == []
    assert result["_loosen_applied"] is True


def test_broaden_knn_doubles_candidate_count():
    result = apply_retrieval_strategy(
        RepairStrategy.BROADEN_KNN,
        {"retrieval_profile": {"candidate_count_pre_rerank": 40}},
    )
    assert result["retrieval_profile"]["candidate_count_pre_rerank"] == 80
    assert result["_broaden_applied"] is True


def test_broaden_knn_caps_at_200():
    result = apply_retrieval_strategy(
        RepairStrategy.BROADEN_KNN,
        {"retrieval_profile": {"candidate_count_pre_rerank": 150}},
    )
    assert result["retrieval_profile"]["candidate_count_pre_rerank"] == 200


def test_broaden_knn_defaults_to_40_when_missing():
    result = apply_retrieval_strategy(
        RepairStrategy.BROADEN_KNN,
        {"retrieval_profile": {}},
    )
    assert result["retrieval_profile"]["candidate_count_pre_rerank"] == 80


def test_enable_fuzzy_entity_sets_flag():
    result = apply_retrieval_strategy(
        RepairStrategy.ENABLE_FUZZY_ENTITY,
        {"retrieval_filters": {}},
    )
    assert result["retrieval_filters"]["fuzzy_entity_matching"] is True


@pytest.mark.parametrize("current,expected", [
    (0, 500.0),
    (500, 1000.0),
    (1000, 2000.0),
    (2000, 4000.0),
    (4000, 5000.0),   # capped
    (10000, 5000.0),  # already over cap → stays at 5000
])
def test_add_spatial_buffer_ladders_correctly(current, expected):
    result = apply_retrieval_strategy(
        RepairStrategy.ADD_SPATIAL_BUFFER,
        {"retrieval_filters": {"spatial_buffer_m": current}},
    )
    assert result["retrieval_filters"]["spatial_buffer_m"] == expected


def test_transform_crs_sets_coerce_flag():
    result = apply_retrieval_strategy(
        RepairStrategy.TRANSFORM_CRS,
        {"retrieval_filters": {}},
    )
    assert result["retrieval_filters"]["coerce_input_crs_to_target"] is True


def test_increase_graph_depth_increments():
    result = apply_retrieval_strategy(
        RepairStrategy.INCREASE_GRAPH_DEPTH,
        {"retrieval_profile": {"graph_max_hops": 2}},
    )
    assert result["retrieval_profile"]["graph_max_hops"] == 3


def test_increase_graph_depth_caps_at_5():
    result = apply_retrieval_strategy(
        RepairStrategy.INCREASE_GRAPH_DEPTH,
        {"retrieval_profile": {"graph_max_hops": 5}},
    )
    assert result["retrieval_profile"]["graph_max_hops"] == 5


def test_terminal_strategy_returns_empty_dict():
    """Terminal strategies aren't Stage-4-eligible."""
    for strategy in (
        RepairStrategy.SURFACE_CONFLICT,
        RepairStrategy.ASK_FOR_DISAMBIGUATION,
        RepairStrategy.REFUSE_OUT_OF_SCOPE,
    ):
        assert apply_retrieval_strategy(strategy, {}) == {}


def test_llm_strategy_returns_empty_dict_for_retrieval_function():
    """REPHRASE / CITATION_RETRY belong to Stage 3, not Stage 4."""
    assert apply_retrieval_strategy(
        RepairStrategy.REPHRASE_NUMERIC_CLAIM, {},
    ) == {}
    assert apply_retrieval_strategy(
        RepairStrategy.REQUEST_CITATION_RETRY, {},
    ) == {}


def test_unknown_strategy_returns_empty_dict_safely():
    """Forward-compat — passing a non-RepairStrategy returns {}."""
    assert apply_retrieval_strategy("NOT_A_STRATEGY", {}) == {}  # type: ignore[arg-type]
    assert apply_retrieval_strategy(None, {}) == {}  # type: ignore[arg-type]


def test_apply_retrieval_strategy_handles_missing_fields():
    """An empty state_snapshot shouldn't crash any strategy."""
    for strategy in RepairStrategy:
        # All strategies must accept an empty snapshot and return
        # SOMETHING (dict — empty or with defaults).
        result = apply_retrieval_strategy(strategy, {})
        assert isinstance(result, dict)
