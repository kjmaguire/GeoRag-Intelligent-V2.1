"""Unit tests for plan §4b repair-strategy dispatcher."""

from __future__ import annotations

import pytest

from app.agent.guards import GuardErrorCode
from app.agent.repair_strategy import (
    STRATEGY_FOR_CODE,
    TERMINAL_STRATEGIES,
    RepairPlan,
    RepairStrategy,
    plan_repair,
)


# ---------------------------------------------------------------------------
# Mapping coverage — locks the table content
# ---------------------------------------------------------------------------


def test_every_guard_error_code_has_a_mapping():
    """Every code defined in :class:`GuardErrorCode` must have at least
    one RepairStrategy. Forward-compat guard: if a new code is added
    upstream without a strategy, this test breaks LOUDLY."""
    for code in GuardErrorCode:
        assert code in STRATEGY_FOR_CODE, (
            f"GuardErrorCode.{code.name} has no entry in STRATEGY_FOR_CODE"
        )
        strategies = STRATEGY_FOR_CODE[code]
        assert len(strategies) >= 1, (
            f"{code.name} mapped to empty strategy tuple"
        )


def test_terminal_strategies_set_lists_every_terminal_strategy():
    """Regression — TERMINAL_STRATEGIES must include exactly the 5
    user-facing strategies the plan §4b spec calls out as terminal."""
    assert TERMINAL_STRATEGIES == frozenset({
        RepairStrategy.ASK_FOR_DISAMBIGUATION,
        RepairStrategy.SURFACE_CONFLICT,
        RepairStrategy.REQUEST_UNIT_CLARIFICATION,
        RepairStrategy.REQUEST_DEPTH_CLARIFICATION,
        RepairStrategy.REFUSE_OUT_OF_SCOPE,
    })


@pytest.mark.parametrize("code,expected_strategy", [
    (GuardErrorCode.NO_EVIDENCE_FOUND, RepairStrategy.LOOSEN_FILTERS),
    (GuardErrorCode.ENTITY_NOT_FOUND, RepairStrategy.ENABLE_FUZZY_ENTITY),
    (GuardErrorCode.AMBIGUOUS_HOLE_ID, RepairStrategy.ASK_FOR_DISAMBIGUATION),
    (GuardErrorCode.AMBIGUOUS_FORMATION_NAME, RepairStrategy.ASK_FOR_DISAMBIGUATION),
    (GuardErrorCode.AMBIGUOUS_PROPERTY_NAME, RepairStrategy.ASK_FOR_DISAMBIGUATION),
    (GuardErrorCode.OVER_FILTERED_QUERY, RepairStrategy.LOOSEN_FILTERS),
    (GuardErrorCode.SPATIAL_QUERY_EMPTY, RepairStrategy.ADD_SPATIAL_BUFFER),
    (GuardErrorCode.SPATIAL_CRS_MISMATCH, RepairStrategy.TRANSFORM_CRS),
    (GuardErrorCode.GRAPH_PATH_NOT_FOUND, RepairStrategy.INCREASE_GRAPH_DEPTH),
    (GuardErrorCode.NUMERIC_GROUNDING_FAILED, RepairStrategy.REPHRASE_NUMERIC_CLAIM),
    (GuardErrorCode.CITATION_INCOMPLETE, RepairStrategy.REQUEST_CITATION_RETRY),
    (GuardErrorCode.CONFLICTING_SOURCES, RepairStrategy.SURFACE_CONFLICT),
    (GuardErrorCode.MISSING_DEPTH_INTERVAL, RepairStrategy.REQUEST_DEPTH_CLARIFICATION),
    (GuardErrorCode.MISSING_ASSAY_UNITS, RepairStrategy.REQUEST_UNIT_CLARIFICATION),
    (GuardErrorCode.SOURCE_SCOPE_VIOLATION, RepairStrategy.REFUSE_OUT_OF_SCOPE),
    (GuardErrorCode.UNSUPPORTED_QUERY_TYPE, RepairStrategy.REFUSE_OUT_OF_SCOPE),
])
def test_code_maps_to_expected_first_strategy(code, expected_strategy):
    """The FIRST strategy in each code's tuple is the one the orchestrator
    tries by default — lock it explicitly."""
    assert STRATEGY_FOR_CODE[code][0] == expected_strategy


def test_no_evidence_found_has_a_fallback_strategy():
    """NO_EVIDENCE_FOUND has two strategies — LOOSEN then BROADEN_KNN."""
    assert STRATEGY_FOR_CODE[GuardErrorCode.NO_EVIDENCE_FOUND] == (
        RepairStrategy.LOOSEN_FILTERS,
        RepairStrategy.BROADEN_KNN,
    )


def test_spatial_query_empty_has_a_fallback_strategy():
    assert STRATEGY_FOR_CODE[GuardErrorCode.SPATIAL_QUERY_EMPTY] == (
        RepairStrategy.ADD_SPATIAL_BUFFER,
        RepairStrategy.LOOSEN_FILTERS,
    )


# ---------------------------------------------------------------------------
# plan_repair — empty inputs / no-op paths
# ---------------------------------------------------------------------------


def test_no_codes_returns_empty_plan():
    plan = plan_repair([])
    assert isinstance(plan, RepairPlan)
    assert plan.strategies == []
    assert plan.terminal is False
    assert plan.is_empty() is True
    assert plan.first_strategy() is None


def test_unknown_code_string_is_dropped_silently():
    plan = plan_repair(["NOT_A_REAL_CODE"])
    assert plan.strategies == []
    assert plan.terminal is False


def test_accepts_mixed_enum_and_string_codes():
    plan = plan_repair([
        GuardErrorCode.NO_EVIDENCE_FOUND,
        "ENTITY_NOT_FOUND",  # string form ok
    ])
    assert RepairStrategy.LOOSEN_FILTERS in plan.strategies
    assert RepairStrategy.ENABLE_FUZZY_ENTITY in plan.strategies


# ---------------------------------------------------------------------------
# plan_repair — single-code paths
# ---------------------------------------------------------------------------


def test_no_evidence_returns_two_loop_friendly_strategies():
    plan = plan_repair([GuardErrorCode.NO_EVIDENCE_FOUND])
    assert plan.strategies == [
        RepairStrategy.LOOSEN_FILTERS,
        RepairStrategy.BROADEN_KNN,
    ]
    assert plan.terminal is False


def test_terminal_code_returns_terminal_plan():
    plan = plan_repair([GuardErrorCode.AMBIGUOUS_HOLE_ID])
    assert plan.strategies == [RepairStrategy.ASK_FOR_DISAMBIGUATION]
    assert plan.terminal is True
    assert "ASK_FOR_DISAMBIGUATION" in (plan.reason or "")


def test_conflicting_sources_is_terminal_surface_conflict():
    """Global Invariant 7: never silently pick a winner."""
    plan = plan_repair([GuardErrorCode.CONFLICTING_SOURCES])
    assert plan.strategies == [RepairStrategy.SURFACE_CONFLICT]
    assert plan.terminal is True


def test_source_scope_violation_refuses_out_of_scope():
    plan = plan_repair([GuardErrorCode.SOURCE_SCOPE_VIOLATION])
    assert plan.strategies == [RepairStrategy.REFUSE_OUT_OF_SCOPE]
    assert plan.terminal is True


# ---------------------------------------------------------------------------
# plan_repair — multi-code paths
# ---------------------------------------------------------------------------


def test_multiple_loop_friendly_codes_dedupe_strategies():
    """Two codes that share a strategy (LOOSEN_FILTERS) yield it once."""
    plan = plan_repair([
        GuardErrorCode.NO_EVIDENCE_FOUND,    # LOOSEN_FILTERS, BROADEN_KNN
        GuardErrorCode.OVER_FILTERED_QUERY,  # LOOSEN_FILTERS  (dup)
    ])
    assert plan.strategies.count(RepairStrategy.LOOSEN_FILTERS) == 1
    assert RepairStrategy.BROADEN_KNN in plan.strategies
    assert plan.terminal is False


def test_terminal_code_truncates_subsequent_loop_friendly_codes():
    """When code-order is [loop-friendly, TERMINAL, loop-friendly], the
    terminal strategy ENDS the walk — no strategies past it are added."""
    plan = plan_repair([
        GuardErrorCode.NO_EVIDENCE_FOUND,        # LOOSEN, BROADEN
        GuardErrorCode.CONFLICTING_SOURCES,      # SURFACE_CONFLICT (terminal)
        GuardErrorCode.ENTITY_NOT_FOUND,         # would add ENABLE_FUZZY
    ])
    assert RepairStrategy.LOOSEN_FILTERS in plan.strategies
    assert RepairStrategy.BROADEN_KNN in plan.strategies
    assert RepairStrategy.SURFACE_CONFLICT in plan.strategies
    # Entity-fuzzy was AFTER the terminal in code order → excluded.
    assert RepairStrategy.ENABLE_FUZZY_ENTITY not in plan.strategies
    assert plan.terminal is True


def test_terminal_code_first_short_circuits_immediately():
    plan = plan_repair([
        GuardErrorCode.AMBIGUOUS_HOLE_ID,          # terminal
        GuardErrorCode.NO_EVIDENCE_FOUND,          # would add LOOSEN
    ])
    assert plan.strategies == [RepairStrategy.ASK_FOR_DISAMBIGUATION]
    assert plan.terminal is True


# ---------------------------------------------------------------------------
# plan_repair — prior_strategies handling
# ---------------------------------------------------------------------------


def test_prior_strategies_are_excluded_from_plan():
    plan = plan_repair(
        [GuardErrorCode.NO_EVIDENCE_FOUND],
        prior_strategies=[RepairStrategy.LOOSEN_FILTERS],
    )
    assert RepairStrategy.LOOSEN_FILTERS not in plan.strategies
    assert RepairStrategy.BROADEN_KNN in plan.strategies
    assert plan.terminal is False


def test_all_strategies_already_tried_returns_terminal_empty():
    """When every loop-friendly strategy for the active codes has been
    tried but max_attempts hasn't been reached yet, the dispatcher
    returns an empty strategies list + terminal=True with a "no fresh
    strategies remain" reason."""
    plan = plan_repair(
        [GuardErrorCode.NO_EVIDENCE_FOUND],
        prior_strategies=[
            RepairStrategy.LOOSEN_FILTERS,
            RepairStrategy.BROADEN_KNN,
        ],
        # Generous max_attempts so the no-fresh-strategies path fires
        # BEFORE the max-attempts hard stop.
        max_attempts=10,
    )
    assert plan.strategies == []
    assert plan.terminal is True
    assert "no fresh strategies" in (plan.reason or "")


def test_prior_strategies_accept_string_form():
    plan = plan_repair(
        [GuardErrorCode.NO_EVIDENCE_FOUND],
        prior_strategies=["LOOSEN_FILTERS"],
    )
    assert RepairStrategy.LOOSEN_FILTERS not in plan.strategies


def test_unknown_prior_strategy_is_dropped_silently():
    plan = plan_repair(
        [GuardErrorCode.NO_EVIDENCE_FOUND],
        prior_strategies=["NOT_A_STRATEGY"],
    )
    # The unknown prior is ignored; both LOOSEN + BROADEN are available.
    assert RepairStrategy.LOOSEN_FILTERS in plan.strategies
    assert RepairStrategy.BROADEN_KNN in plan.strategies


def test_exhausted_strategies_reported_back():
    plan = plan_repair(
        [GuardErrorCode.NO_EVIDENCE_FOUND],
        prior_strategies=[RepairStrategy.LOOSEN_FILTERS],
    )
    assert plan.exhausted_strategies == [RepairStrategy.LOOSEN_FILTERS]


# ---------------------------------------------------------------------------
# plan_repair — max_attempts
# ---------------------------------------------------------------------------


def test_max_attempts_zero_refuses_immediately():
    plan = plan_repair(
        [GuardErrorCode.NO_EVIDENCE_FOUND],
        max_attempts=0,
    )
    assert plan.strategies == [RepairStrategy.REFUSE_OUT_OF_SCOPE]
    assert plan.terminal is True
    assert "max_attempts" in (plan.reason or "")


def test_max_attempts_exhausted_refuses():
    plan = plan_repair(
        [GuardErrorCode.NO_EVIDENCE_FOUND],
        max_attempts=2,
        prior_strategies=[
            RepairStrategy.LOOSEN_FILTERS,
            RepairStrategy.BROADEN_KNN,
        ],
    )
    assert plan.strategies == [RepairStrategy.REFUSE_OUT_OF_SCOPE]
    assert plan.terminal is True


def test_max_attempts_not_yet_reached_keeps_planning():
    plan = plan_repair(
        [GuardErrorCode.NO_EVIDENCE_FOUND],
        max_attempts=3,
        prior_strategies=[RepairStrategy.LOOSEN_FILTERS],
    )
    # 1 prior + max=3 → 2 more attempts allowed.
    assert RepairStrategy.BROADEN_KNN in plan.strategies
    assert plan.strategies != [RepairStrategy.REFUSE_OUT_OF_SCOPE]


# ---------------------------------------------------------------------------
# Plan-shape helpers
# ---------------------------------------------------------------------------


def test_first_strategy_returns_first_non_terminal():
    plan = plan_repair([GuardErrorCode.NO_EVIDENCE_FOUND])
    assert plan.first_strategy() == RepairStrategy.LOOSEN_FILTERS


def test_first_strategy_returns_none_when_empty():
    plan = plan_repair([])
    assert plan.first_strategy() is None


def test_is_empty_aligns_with_strategies_list():
    full = plan_repair([GuardErrorCode.NO_EVIDENCE_FOUND])
    empty = plan_repair([])
    assert full.is_empty() is False
    assert empty.is_empty() is True


# ---------------------------------------------------------------------------
# Immutability — RepairPlan is frozen
# ---------------------------------------------------------------------------


def test_repair_plan_is_frozen():
    plan = plan_repair([GuardErrorCode.NO_EVIDENCE_FOUND])
    with pytest.raises(Exception):
        # dataclass(frozen=True) raises FrozenInstanceError, but the
        # exact class lives in dataclasses; broad assertion is fine here.
        plan.terminal = True  # type: ignore[misc]
