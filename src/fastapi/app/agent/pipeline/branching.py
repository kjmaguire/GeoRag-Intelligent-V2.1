"""pipeline/branching.py — Phase 3 D4 branching helpers.

Extracted from ``app.agent.orchestrator`` during the Wave 2.C refactor.
The orchestrator delegates to these helpers; the public entry point
``run_deterministic_rag`` stays in ``app.agent.orchestrator``.

Functions owned here
--------------------
_record_decision             append a Decision to plan.decisions
_decide_post_decomposition   D4 branch: post_decomposition
_decide_post_retrieval       D4 branch: post_retrieval
_decide_post_binding         D4 branch: post_binding
_emit_agentic_decision       emit SSE agentic_decision event

Decision branches (locked per D4, 2026-04-29):

  post_decomposition
    proceed_with_plan         plan has >=1 sub_query; execute agentic path
    fallback_linear           empty plan or trigger suppressed -> §04h path
    escalate_continued_empty  prior turn also returned empty; Phase 4 hook

  post_retrieval
    synthesize_now       every sub_query ok
    partial_synthesize   >=1 ok + >=1 empty/error/timeout
    refuse_insufficient  every sub_query non-ok

  post_binding
    accept          all claims have >=1 citation
    revise_once     >=1 unbound + revise budget remains (Phase 4 wires call)
    refuse_unbound  unbound claims + no budget left; refuse path
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.conversation_state import ConversationState
    from app.models.decomposition import DecompositionPlan

logger = logging.getLogger(__name__)


def _record_decision(
    plan: DecompositionPlan | None,
    point: str,
    branch_taken: str,
    rationale: str | None = None,
) -> None:
    """Append a Decision record to plan.decisions.

    No-op when plan is None (linear path carries no plan object).
    Never raises — decision recording is observability only; it must not
    break the orchestrator under any circumstances.

    Args:
        plan:         The current DecompositionPlan, or None on the linear path.
        point:        Which of the three D4 decision points triggered this call.
        branch_taken: Literal branch name chosen at this point.
        rationale:    Optional human-readable explanation for debug/replay.
    """
    if plan is None:
        return
    try:
        from app.models.decomposition import Decision  # noqa: PLC0415
        plan.decisions.append(
            Decision(
                point=point,
                branch_taken=branch_taken,
                rationale=rationale,
                decided_at=datetime.now(UTC),
            )
        )
    except Exception:
        logger.debug(
            "_record_decision: failed to append decision point=%s branch=%s (non-fatal)",
            point,
            branch_taken,
            exc_info=True,
        )


def _decide_post_decomposition(
    plan: DecompositionPlan | None,
    prior_state: ConversationState | None,
) -> str:
    """Return the post_decomposition branch name.

    Args:
        plan:        The DecompositionPlan returned by _maybe_run_agentic_decomposition,
                     or None when decomposition was suppressed or errored.
        prior_state: ConversationState from _maybe_read_and_resolve_anaphora, or None.

    Returns:
        'proceed_with_plan'        — plan has >=1 sub_query; run agentic path.
        'fallback_linear'          — plan is None or empty; use §04h linear path.
        'escalate_continued_empty' — prior turn also returned empty (pending_followup
                                     set) AND decomposition produced no sub-queries.
    """
    try:
        if plan is None or len(plan.sub_queries) == 0:
            if (
                prior_state is not None
                and getattr(prior_state, "pending_followup", None) is not None
            ):
                return "escalate_continued_empty"
            return "fallback_linear"
        return "proceed_with_plan"
    except Exception:
        logger.debug(
            "_decide_post_decomposition: unexpected error (defaulting to fallback_linear)",
            exc_info=True,
        )
        return "fallback_linear"


def _decide_post_retrieval(
    plan: DecompositionPlan | None,
) -> str:
    """Return the post_retrieval branch name.

    Args:
        plan: The DecompositionPlan after execute_plan has set sub_query.outcome
              on each sub-query.  None -> synthesize_now (linear path, no plan).

    Returns:
        'synthesize_now'       — every sub_query.outcome == "ok" with results.
        'partial_synthesize'   — >=1 "ok" + >=1 "empty"/"error"/"timeout".
        'refuse_insufficient'  — every sub_query is "empty", "error", or "timeout".
    """
    try:
        if plan is None or len(plan.sub_queries) == 0:
            return "synthesize_now"

        ok_count = sum(1 for sq in plan.sub_queries if sq.outcome == "ok")
        total = len(plan.sub_queries)

        if ok_count == total:
            return "synthesize_now"
        if ok_count > 0:
            return "partial_synthesize"
        return "refuse_insufficient"
    except Exception:
        logger.debug(
            "_decide_post_retrieval: unexpected error (defaulting to refuse_insufficient)",
            exc_info=True,
        )
        return "refuse_insufficient"


def _decide_post_binding(
    claims_with_citations: int,
    claims_without_citations: int,
    revise_budget_remaining: int,
) -> str:
    """Return the post_binding branch name.

    Args:
        claims_with_citations:    Count of claims that have >=1 citation bound.
        claims_without_citations: Count of claims with no citation bound.
        revise_budget_remaining:  Number of revise passes still available (0 or 1 per D5).

    Returns:
        'accept'         — all claims have >=1 citation; response is fully grounded.
        'revise_once'    — >=1 unbound claim AND revise_budget_remaining > 0.
                           Phase 3 records the decision and falls through to refuse;
                           Phase 4 wires the actual revise-and-rebind call.
        'refuse_unbound' — >=1 unbound claim AND no revise budget left; refuse path.
    """
    try:
        if claims_without_citations <= 0:
            return "accept"
        if revise_budget_remaining > 0:
            return "revise_once"
        return "refuse_unbound"
    except Exception:
        logger.debug(
            "_decide_post_binding: unexpected error (defaulting to refuse_unbound)",
            exc_info=True,
        )
        return "refuse_unbound"


async def _emit_agentic_decision(
    emit: Callable[[str], Awaitable[None]] | None,
    point: str,
    branch_taken: str,
    rationale: str | None,
) -> None:
    """Emit an ``agentic_decision`` SSE event if an emit callback is present.

    Follows the §07c SSE event convention: JSON object with a ``type`` field.
    Non-raising — an emit failure must never break the orchestrator.

    Args:
        emit:         Optional async callable accepting a string SSE payload.
        point:        D4 decision point name.
        branch_taken: Branch chosen at this point.
        rationale:    Optional explanation.
    """
    if emit is None:
        return
    try:
        payload = json.dumps(
            {
                "type": "agentic_decision",
                "kind": "d4_branch",
                "point": point,
                "branch_taken": branch_taken,
                "rationale": rationale,
            }
        )
        await emit(payload)
    except Exception:
        logger.debug(
            "_emit_agentic_decision: emit failed (non-fatal) point=%s branch=%s",
            point,
            branch_taken,
            exc_info=True,
        )
