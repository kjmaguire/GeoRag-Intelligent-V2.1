"""Plan §4b — repair-strategy dispatcher.

Given a list of :class:`GuardErrorCode` values from a failed (or
demoted) answer pass, return a :class:`RepairPlan` containing the
ordered list of :class:`RepairStrategy` values the orchestrator should
attempt next — plus a ``terminal`` flag indicating whether the plan
is loop-friendly (re-run the graph) or loop-ending (surface a prompt
to the user / refuse).

This is the layer between ``app.agent.guards.classify_guards`` (which
turns warning strings into typed codes) and the future orchestrator
loop that will actually execute the strategies. Wiring lives in a
separate session — this module ships the **decision logic** in pure
form so the strategy ordering can be tuned, A/B-tested, and locked
with unit tests before any I/O is involved.

The mapping tables verbatim from plan §4b:

  Retrieval-failure codes
    NO_EVIDENCE_FOUND        → LOOSEN_FILTERS, BROADEN_KNN
    ENTITY_NOT_FOUND         → ENABLE_FUZZY_ENTITY
    AMBIGUOUS_HOLE_ID        → ASK_FOR_DISAMBIGUATION (terminal)
    AMBIGUOUS_FORMATION_NAME → ASK_FOR_DISAMBIGUATION (terminal)
    AMBIGUOUS_PROPERTY_NAME  → ASK_FOR_DISAMBIGUATION (terminal)
    OVER_FILTERED_QUERY      → LOOSEN_FILTERS
    SPATIAL_QUERY_EMPTY      → ADD_SPATIAL_BUFFER, LOOSEN_FILTERS
    SPATIAL_CRS_MISMATCH     → TRANSFORM_CRS
    GRAPH_PATH_NOT_FOUND     → INCREASE_GRAPH_DEPTH

  Evidence-quality codes
    NUMERIC_GROUNDING_FAILED → REPHRASE_NUMERIC_CLAIM
    CITATION_INCOMPLETE      → REQUEST_CITATION_RETRY
    CONFLICTING_SOURCES      → SURFACE_CONFLICT (terminal — never
                                   silently pick a winner; Global
                                   Invariant 7)
    MISSING_DEPTH_INTERVAL   → REQUEST_DEPTH_CLARIFICATION (terminal)
    MISSING_ASSAY_UNITS      → REQUEST_UNIT_CLARIFICATION (terminal)
    SOURCE_SCOPE_VIOLATION   → REFUSE_OUT_OF_SCOPE (terminal)

  Query-failure codes
    UNSUPPORTED_QUERY_TYPE   → REFUSE_OUT_OF_SCOPE (terminal)

Pure module: no I/O, no DB, no LLM. Side-effect-free.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from app.agent.guards import GuardErrorCode

logger = logging.getLogger(__name__)


__all__ = [
    "RepairStrategy",
    "TERMINAL_STRATEGIES",
    "STRATEGY_FOR_CODE",
    "RepairPlan",
    "plan_repair",
]


# ---------------------------------------------------------------------------
# RepairStrategy enum
# ---------------------------------------------------------------------------


class RepairStrategy(StrEnum):
    """Plan §4b — actions the orchestrator can take when guards fire.

    Two flavours:
      - **Loop-friendly** strategies modify the retrieval / generation
        plan and re-issue the graph (LOOSEN_FILTERS, BROADEN_KNN,
        ENABLE_FUZZY_ENTITY, ADD_SPATIAL_BUFFER, TRANSFORM_CRS,
        INCREASE_GRAPH_DEPTH, REPHRASE_NUMERIC_CLAIM,
        REQUEST_CITATION_RETRY).
      - **Terminal** strategies surface a user-facing prompt (or refusal)
        and END the repair loop. See :data:`TERMINAL_STRATEGIES`.

    The orchestrator MUST NOT silently pick a winner on CONFLICTING_SOURCES
    (Global Invariant 7). The terminal SURFACE_CONFLICT strategy renders
    side-by-side evidence and lets the geologist decide.
    """

    # ── Retrieval-side, loop-friendly ───────────────────────────────────
    LOOSEN_FILTERS = "LOOSEN_FILTERS"
    BROADEN_KNN = "BROADEN_KNN"
    ENABLE_FUZZY_ENTITY = "ENABLE_FUZZY_ENTITY"
    ADD_SPATIAL_BUFFER = "ADD_SPATIAL_BUFFER"
    TRANSFORM_CRS = "TRANSFORM_CRS"
    INCREASE_GRAPH_DEPTH = "INCREASE_GRAPH_DEPTH"
    # ── Generation-side, loop-friendly ──────────────────────────────────
    REPHRASE_NUMERIC_CLAIM = "REPHRASE_NUMERIC_CLAIM"
    REQUEST_CITATION_RETRY = "REQUEST_CITATION_RETRY"
    # ── Terminal — surface UI prompt to user, end loop ──────────────────
    ASK_FOR_DISAMBIGUATION = "ASK_FOR_DISAMBIGUATION"
    SURFACE_CONFLICT = "SURFACE_CONFLICT"
    REQUEST_UNIT_CLARIFICATION = "REQUEST_UNIT_CLARIFICATION"
    REQUEST_DEPTH_CLARIFICATION = "REQUEST_DEPTH_CLARIFICATION"
    REFUSE_OUT_OF_SCOPE = "REFUSE_OUT_OF_SCOPE"


TERMINAL_STRATEGIES: frozenset[RepairStrategy] = frozenset({
    RepairStrategy.ASK_FOR_DISAMBIGUATION,
    RepairStrategy.SURFACE_CONFLICT,
    RepairStrategy.REQUEST_UNIT_CLARIFICATION,
    RepairStrategy.REQUEST_DEPTH_CLARIFICATION,
    RepairStrategy.REFUSE_OUT_OF_SCOPE,
})


# ---------------------------------------------------------------------------
# Code → strategy table
# ---------------------------------------------------------------------------
#
# Order WITHIN each tuple matters: the first strategy is what the
# orchestrator tries first. When multiple codes fire on the same pass,
# `plan_repair` deduplicates strategies but otherwise preserves the
# code-order from the input, which itself comes from
# `classify_guards` (where layer-3 warnings are emitted before
# layer-5, etc.).


STRATEGY_FOR_CODE: dict[GuardErrorCode, tuple[RepairStrategy, ...]] = {
    # Retrieval-failure codes
    GuardErrorCode.NO_EVIDENCE_FOUND: (
        RepairStrategy.LOOSEN_FILTERS,
        RepairStrategy.BROADEN_KNN,
    ),
    GuardErrorCode.ENTITY_NOT_FOUND: (
        RepairStrategy.ENABLE_FUZZY_ENTITY,
    ),
    GuardErrorCode.AMBIGUOUS_HOLE_ID: (
        RepairStrategy.ASK_FOR_DISAMBIGUATION,
    ),
    GuardErrorCode.AMBIGUOUS_FORMATION_NAME: (
        RepairStrategy.ASK_FOR_DISAMBIGUATION,
    ),
    GuardErrorCode.AMBIGUOUS_PROPERTY_NAME: (
        RepairStrategy.ASK_FOR_DISAMBIGUATION,
    ),
    GuardErrorCode.OVER_FILTERED_QUERY: (
        RepairStrategy.LOOSEN_FILTERS,
    ),
    GuardErrorCode.SPATIAL_QUERY_EMPTY: (
        RepairStrategy.ADD_SPATIAL_BUFFER,
        RepairStrategy.LOOSEN_FILTERS,
    ),
    GuardErrorCode.SPATIAL_CRS_MISMATCH: (
        RepairStrategy.TRANSFORM_CRS,
    ),
    GuardErrorCode.GRAPH_PATH_NOT_FOUND: (
        RepairStrategy.INCREASE_GRAPH_DEPTH,
    ),
    # Evidence-quality codes
    GuardErrorCode.NUMERIC_GROUNDING_FAILED: (
        RepairStrategy.REPHRASE_NUMERIC_CLAIM,
    ),
    GuardErrorCode.CITATION_INCOMPLETE: (
        RepairStrategy.REQUEST_CITATION_RETRY,
    ),
    GuardErrorCode.CONFLICTING_SOURCES: (
        RepairStrategy.SURFACE_CONFLICT,
    ),
    GuardErrorCode.MISSING_DEPTH_INTERVAL: (
        RepairStrategy.REQUEST_DEPTH_CLARIFICATION,
    ),
    GuardErrorCode.MISSING_ASSAY_UNITS: (
        RepairStrategy.REQUEST_UNIT_CLARIFICATION,
    ),
    GuardErrorCode.SOURCE_SCOPE_VIOLATION: (
        RepairStrategy.REFUSE_OUT_OF_SCOPE,
    ),
    # Query-failure
    GuardErrorCode.UNSUPPORTED_QUERY_TYPE: (
        RepairStrategy.REFUSE_OUT_OF_SCOPE,
    ),
}


# ---------------------------------------------------------------------------
# RepairPlan result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairPlan:
    """Ordered list of strategies + meta about the plan.

    Fields:
        strategies: The strategies to apply, in execution order. Empty
            list when there's nothing to repair (no guard codes fired,
            or every code maps to nothing actionable).
        terminal: True when the plan ENDS the repair loop after these
            strategies execute — either because a terminal strategy is
            in the list, OR ``max_attempts`` has been exhausted, OR no
            loop-friendly strategy remains to try.
        reason: Short human-readable reason for the terminal flag.
            None when ``terminal=False``.
        exhausted_strategies: Strategies the caller said it has already
            tried (``prior_strategies`` input). They're EXCLUDED from
            ``strategies`` so the orchestrator doesn't re-run something
            that just failed. Returned here for trace visibility.
    """

    strategies: list[RepairStrategy] = field(default_factory=list)
    terminal: bool = False
    reason: str | None = None
    exhausted_strategies: list[RepairStrategy] = field(default_factory=list)

    def is_empty(self) -> bool:
        """No work to do — orchestrator returns the current answer."""
        return not self.strategies

    def first_strategy(self) -> RepairStrategy | None:
        """Convenience for the loop driver that runs one strategy at a time."""
        return self.strategies[0] if self.strategies else None


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def plan_repair(
    codes: Iterable[GuardErrorCode | str],
    *,
    max_attempts: int = 2,
    prior_strategies: Iterable[RepairStrategy | str] = (),
) -> RepairPlan:
    """Plan the next-attempt repair given the guards that fired.

    Args:
        codes: Iterable of :class:`GuardErrorCode` values OR their
            string ``.value`` form (the trace stores ``.value`` strings,
            and the orchestrator may round-trip through JSON).
        max_attempts: Maximum total repair attempts before the loop
            forcibly terminates with REFUSE_OUT_OF_SCOPE. The check
            uses ``len(prior_strategies) >= max_attempts``.
        prior_strategies: Strategies the orchestrator has ALREADY tried
            on this query. They're excluded from ``strategies`` (no
            point re-running what just failed) and reported back via
            ``exhausted_strategies`` for trace logging.

    Returns:
        A :class:`RepairPlan` with the strategies to attempt next, in
        execution order. Empty + non-terminal when no guards fired.

    Behaviour notes:
        - Unknown / unrecognised codes are silently dropped (forward-
          compat with new GuardErrorCode values added in the future
          before this module is updated).
        - When ANY input code maps to a terminal strategy, the plan
          truncates at that strategy and sets ``terminal=True``. The
          ordering inside ``codes`` determines which terminal strategy
          wins.
        - When ``max_attempts`` has been reached, the plan returns
          ``[REFUSE_OUT_OF_SCOPE]`` + ``terminal=True`` regardless of
          what codes are present.
        - When the only useful strategies for the current codes are
          already in ``prior_strategies``, the plan returns an empty
          ``strategies`` list with ``terminal=True`` + a reason —
          the orchestrator has no fresh moves to try.
    """
    # 1. Normalise inputs to enum form. Unknown strings are dropped.
    codes_typed = list(_coerce_codes(codes))
    prior_set = set(_coerce_strategies(prior_strategies))

    # 2. Max-attempts hard stop. The +0 check makes the function safe
    # to call with prior_strategies=[] and max_attempts=0 → terminal
    # immediately (orchestrator effectively disables repair).
    if max_attempts <= 0 or len(prior_set) >= max_attempts:
        return RepairPlan(
            strategies=[RepairStrategy.REFUSE_OUT_OF_SCOPE],
            terminal=True,
            reason=(
                f"max_attempts={max_attempts} exhausted "
                f"(prior_strategies={len(prior_set)})"
            ),
            exhausted_strategies=sorted(prior_set, key=lambda s: s.value),
        )

    # 3. No codes fired → no work.
    if not codes_typed:
        return RepairPlan(
            strategies=[],
            terminal=False,
            reason=None,
            exhausted_strategies=sorted(prior_set, key=lambda s: s.value),
        )

    # 4. Build the strategy list in code-order, dedup, exclude prior.
    seen: dict[RepairStrategy, None] = {}
    truncated_at_terminal: RepairStrategy | None = None
    for code in codes_typed:
        for strategy in STRATEGY_FOR_CODE.get(code, ()):
            if strategy in prior_set:
                continue
            if strategy in seen:
                continue
            seen[strategy] = None
            if strategy in TERMINAL_STRATEGIES:
                # Stop walking — a terminal strategy ends the loop.
                truncated_at_terminal = strategy
                break
        if truncated_at_terminal is not None:
            break

    strategies = list(seen.keys())

    # 5. Decide the terminal flag + reason.
    if truncated_at_terminal is not None:
        return RepairPlan(
            strategies=strategies,
            terminal=True,
            reason=f"terminal strategy reached: {truncated_at_terminal.value}",
            exhausted_strategies=sorted(prior_set, key=lambda s: s.value),
        )

    if not strategies:
        # Every strategy that would have applied is already in prior_set.
        # The orchestrator has nothing fresh to try.
        return RepairPlan(
            strategies=[],
            terminal=True,
            reason="no fresh strategies remain (all candidates already tried)",
            exhausted_strategies=sorted(prior_set, key=lambda s: s.value),
        )

    return RepairPlan(
        strategies=strategies,
        terminal=False,
        reason=None,
        exhausted_strategies=sorted(prior_set, key=lambda s: s.value),
    )


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _coerce_codes(
    codes: Iterable[GuardErrorCode | str],
) -> Iterable[GuardErrorCode]:
    """Accept either enum values or their string form. Drop unknowns."""
    for c in codes:
        if isinstance(c, GuardErrorCode):
            yield c
            continue
        if not isinstance(c, str):
            continue
        try:
            yield GuardErrorCode(c)
        except ValueError:
            logger.debug(
                "plan_repair: unknown guard code %r (dropped)", c,
            )


def _coerce_strategies(
    strategies: Iterable[RepairStrategy | str],
) -> Iterable[RepairStrategy]:
    """Same shape as `_coerce_codes` for RepairStrategy."""
    for s in strategies:
        if isinstance(s, RepairStrategy):
            yield s
            continue
        if not isinstance(s, str):
            continue
        try:
            yield RepairStrategy(s)
        except ValueError:
            logger.debug(
                "plan_repair: unknown repair strategy %r (dropped)", s,
            )
