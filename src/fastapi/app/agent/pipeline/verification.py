"""pipeline/verification.py — Phase 4+5 verification helpers.

Extracted from ``app.agent.orchestrator`` during the Wave 2.C refactor.
The orchestrator delegates to these helpers; the public entry point
``run_deterministic_rag`` stays in ``app.agent.orchestrator``.

Functions owned here
--------------------
MAX_REVISE_COUNT              D5 bounded 1-revise budget constant
_build_claim_verifications    build ClaimVerification list from assembled response
_revise_failing_subqueries    Phase 4.B single-bounded retry for failing sub-queries
_escalate_continued_empty     Phase 4.C one-shot escalation for continued-empty turns
_run_spatial_temporal_verification  Phase 5 spatial/temporal claim verification
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.deps import AgentDeps
    from app.models.conversation_state import ConversationState
    from app.models.decomposition import DecompositionPlan
    from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

MAX_REVISE_COUNT: int = 1
"""D5 bounded 1-revise budget per answer_run.  NEVER increase without arch review."""


async def _run_spatial_temporal_verification(
    plan: DecompositionPlan,
    conv_state: ConversationState | None,
    deps: AgentDeps,
    *,
    revise_count: int,
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[DecompositionPlan, bool]:
    """Run Phase 5 spatial and temporal claim verification.

    Iterates over plan.verification records that have guard_status='passed' and
    checks them for spatial / temporal signals using regex-only extraction.
    When geometry or document-date evidence is available, it calls the PostGIS
    helpers in spatial_temporal_verify.py to resolve a consistent/inconsistent/
    indeterminate verdict.

    Any 'inconsistent' verdict is a hard spatial/temporal hallucination signal.
    The needs_revise flag is set True to trigger the Phase 4 revise path.  If
    the D5 revise budget is already exhausted (revise_count == MAX_REVISE_COUNT),
    the offending claim's guard_status is overwritten to 'refused' in-place and
    the appropriate SSE event is emitted instead.

    Status 'indeterminate' is silently accepted — data absence is not a refusal.

    Defensive contract: any exception inside this helper logs and returns the
    original plan + needs_revise=False so the response is never blocked.

    Args:
        plan:         Current DecompositionPlan (mutated in place for refused claims).
        conv_state:   ConversationState from the current turn, or None.
        deps:         AgentDeps for pg_pool access.
        revise_count: How many D5 revise passes have already occurred (0 or 1).
        emit:         Optional SSE emit callback.

    Returns:
        (plan, needs_revise) — plan is the (possibly mutated) plan;
        needs_revise is True when an inconsistency was found AND revise budget
        remains, False otherwise.
    """
    from app.agent.spatial_temporal_verify import (  # noqa: PLC0415
        extract_spatial_signals,
        extract_temporal_signals,
        verify_spatial_claim,
        verify_temporal_claim,
    )
    from app.models.decomposition import (  # noqa: PLC0415
        ClaimSpatialVerification,
        ClaimTemporalVerification,
    )

    needs_revise: bool = False

    try:
        spatial_focus = getattr(conv_state, "spatial_focus", None) if conv_state else None
        temporal_focus = getattr(conv_state, "temporal_focus", None) if conv_state else None
        pg_pool = getattr(deps, "pg_pool", None)

        for cv in plan.verification:
            if getattr(cv, "guard_status", "passed") != "passed":
                continue  # skip already refused / revised claims

            claim_text: str = getattr(cv, "claim_text", "") or ""

            # ---- Spatial branch ----
            spatial_signals = extract_spatial_signals(claim_text)
            if spatial_signals and spatial_focus is not None:
                try:
                    sv: ClaimSpatialVerification = await verify_spatial_claim(
                        cv, spatial_focus, pg_pool
                    )
                    plan.spatial_verifications.append(sv)
                    if sv.status == "inconsistent":
                        logger.info(
                            "_run_spatial_temporal_verification: spatial INCONSISTENT "
                            "claim=%.80s focus=%s distance_m=%s",
                            claim_text,
                            sv.focus_summary,
                            sv.distance_m,
                        )
                        if revise_count < MAX_REVISE_COUNT:
                            needs_revise = True
                        else:
                            # Budget exhausted — mark refused in place.
                            cv.guard_status = "refused"  # type: ignore[assignment]
                            if emit is not None:
                                with contextlib.suppress(Exception):
                                    await emit(
                                        "__agentic_spatial_inconsistent__:"
                                        + json.dumps({
                                            "type": "agentic_spatial_inconsistent",
                                            "claim_text": claim_text[:200],
                                            "focus_summary": sv.focus_summary,
                                            "distance_m": sv.distance_m,
                                        })
                                    )
                except Exception:
                    logger.warning(
                        "_run_spatial_temporal_verification: spatial verify raised "
                        "(non-fatal, treating as indeterminate)",
                        exc_info=True,
                    )
                    plan.spatial_verifications.append(
                        ClaimSpatialVerification(
                            claim_text=claim_text,
                            status="indeterminate",
                            distance_m=None,
                            focus_summary="verify_failed",
                        )
                    )
                    if emit is not None:
                        with contextlib.suppress(Exception):
                            await emit(
                                "__agentic_spatial_verify_failed__:"
                                + json.dumps({
                                    "type": "agentic_spatial_verify_failed",
                                    "claim_text": claim_text[:200],
                                })
                            )

            # ---- Temporal branch ----
            temporal_signals = extract_temporal_signals(claim_text)
            if temporal_signals:
                try:
                    tv: ClaimTemporalVerification = await verify_temporal_claim(
                        cv, temporal_focus, pg_pool
                    )
                    plan.temporal_verifications.append(tv)
                    if tv.status == "inconsistent":
                        logger.info(
                            "_run_spatial_temporal_verification: temporal INCONSISTENT "
                            "claim=%.80s doc_date=%s focus=%s",
                            claim_text,
                            tv.document_date,
                            tv.focus_summary,
                        )
                        if revise_count < MAX_REVISE_COUNT:
                            needs_revise = True
                        else:
                            cv.guard_status = "refused"  # type: ignore[assignment]
                            if emit is not None:
                                with contextlib.suppress(Exception):
                                    await emit(
                                        "__agentic_temporal_inconsistent__:"
                                        + json.dumps({
                                            "type": "agentic_temporal_inconsistent",
                                            "claim_text": claim_text[:200],
                                            "document_date": tv.document_date,
                                            "focus_summary": tv.focus_summary,
                                        })
                                    )
                except Exception:
                    logger.warning(
                        "_run_spatial_temporal_verification: temporal verify raised "
                        "(non-fatal, treating as indeterminate)",
                        exc_info=True,
                    )
                    plan.temporal_verifications.append(
                        ClaimTemporalVerification(
                            claim_text=claim_text,
                            status="indeterminate",
                            document_date=None,
                            focus_summary="verify_failed",
                        )
                    )
                    if emit is not None:
                        with contextlib.suppress(Exception):
                            await emit(
                                "__agentic_temporal_verify_failed__:"
                                + json.dumps({
                                    "type": "agentic_temporal_verify_failed",
                                    "claim_text": claim_text[:200],
                                })
                            )

    except Exception:
        logger.warning(
            "_run_spatial_temporal_verification: outer exception (non-fatal, "
            "returning plan unchanged)",
            exc_info=True,
        )
        needs_revise = False

    return plan, needs_revise


def _build_claim_verifications(
    response: GeoRAGResponse,
    *,
    revise_count: int = 0,
) -> list:
    """Build a list[ClaimVerification] from the assembled response's citation set.

    Pure function — no I/O, no side effects, safe to call in tests without
    any async infrastructure.

    Rules (§04i Layer 2 + D5):
      - A citation with a non-empty source_chunk_id → guard_status='passed'
        (Layer 2 typed output already validated it; we trust it here).
      - A citation with an empty/None source_chunk_id → unbound claim.
        If revise_count == 0: guard_status='revised' (caller will attempt revise).
        If revise_count == 1: guard_status='refused' (budget exhausted).
      - Empty citation list → empty verification list (no claims to verify).

    Args:
        response:     Assembled GeoRAGResponse post validate-and-repair.
        revise_count: How many revise passes have already occurred (0 or 1).

    Returns:
        list[ClaimVerification] — one record per citation, ordered by citation
        appearance in response.citations.
    """
    from app.models.decomposition import ClaimVerification  # noqa: PLC0415

    verifications: list = []
    for cit in response.citations:
        is_bound = bool(getattr(cit, "source_chunk_id", None))
        if is_bound:
            guard_status: str = "passed"
            rc: int = 0
        else:
            guard_status = "refused" if revise_count >= MAX_REVISE_COUNT else "revised"
            rc = revise_count

        # passage_ids: extract from citation.source_chunk_id if it parses as a
        # UUID (Qdrant vector IDs are UUIDs; synthetic IDs like 'silver:collars:...'
        # are not).  Citation has no passage_id field — the dead getattr branch
        # was removed by drift-audit 2026-05-13.
        raw_pid = getattr(cit, "source_chunk_id", None)
        passage_uuid_list: list = []
        if raw_pid:
            try:
                from uuid import UUID as _UUID  # noqa: PLC0415
                passage_uuid_list = [_UUID(str(raw_pid))]
            except (ValueError, AttributeError):
                pass

        # Phase 6.A — propagate source_chunk_id from the citation onto the
        # verification record so Phase 5 spatial/temporal verifiers can resolve
        # geometry and document dates without the old getattr(_hint) workaround.
        # Only 'passed' (bound) claims carry a meaningful source_chunk_id;
        # revised/refused claims set it to None.
        chunk_id_for_verifier: str | None = (
            str(getattr(cit, "source_chunk_id", "")) or None
        ) if is_bound else None

        verifications.append(
            ClaimVerification(
                claim_text=getattr(cit, "document_title", None) or str(getattr(cit, "citation_id", "")),
                guard_status=guard_status,  # type: ignore[arg-type]
                revise_count=rc,  # type: ignore[arg-type]
                passage_ids=passage_uuid_list,
                source_chunk_id=chunk_id_for_verifier,
            )
        )
    return verifications


async def _revise_failing_subqueries(
    plan: DecompositionPlan,
    deps: AgentDeps,
    *,
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> DecompositionPlan:
    """Re-execute sub-queries that produced no usable evidence (empty/error outcome).

    Phase 4.B — single bounded retry for sub-queries that failed or returned
    empty on the first pass.  Heuristic: increase top_k by 50 %, lower
    min_relevance by 0.1 (floor 0.3), and clear document_filter to broaden
    the search.  The original sub_query outcomes are preserved; revised
    sub-queries get a '__revise_attempt: 1' flag in their result dict so the
    plan audit trail reflects the retry.

    Does NOT raise — any exception is caught, logged, and the original plan
    is returned unchanged.  The caller treats a raised exception the same as
    an empty-result revise (falls through to refuse_unbound).

    Args:
        plan:  Current DecompositionPlan; sub_queries may have empty/error outcomes.
        deps:  AgentDeps for execute_plan.
        emit:  Optional SSE emit callback.

    Returns:
        The (mutated) plan with revised sub_query outcomes merged in.
    """
    from app.models.decomposition import (  # noqa: PLC0415
        DocumentPassageSearchInput,
        SubQueryDocumentPassageSearch,
    )

    # Identify sub-queries that need re-running.
    failing_ids = {
        sq.id for sq in plan.sub_queries if sq.outcome in ("empty", "error", "timeout")
    }
    if not failing_ids:
        logger.debug("_revise_failing_subqueries: no failing sub-queries — no-op")
        return plan

    logger.info(
        "_revise_failing_subqueries: revising %d failing sub-query(ies): %s",
        len(failing_ids),
        sorted(failing_ids),
    )

    # For each failing sub-query, widen the search parameters and reset to pending.
    for sq in plan.sub_queries:
        if sq.id not in failing_ids:
            continue
        # Mark metadata so the audit trail shows a revise attempt occurred.
        prior = (sq.error_message or "").strip()
        sq.error_message = (
            f"{prior} | __revise_attempt: 1" if prior else "__revise_attempt: 1"
        )
        # Reset to pending so execute_plan re-runs it.
        sq.outcome = "pending"  # type: ignore[assignment]
        sq.result = None
        sq.started_at = None
        sq.completed_at = None
        # Widen document_passage_search parameters (the most common failing class).
        if isinstance(sq, SubQueryDocumentPassageSearch):
            old_top_k = sq.input.top_k
            old_min_rel = sq.input.min_relevance
            new_top_k = min(50, int(old_top_k * 1.5) or 15)
            new_min_rel = max(0.3, old_min_rel - 0.1)
            sq.input = DocumentPassageSearchInput(
                query_text=sq.input.query_text,
                top_k=new_top_k,
                min_relevance=new_min_rel,
                document_filter=None,  # broadened: drop filter
            )
            logger.debug(
                "_revise_failing_subqueries: sq=%s widened top_k %d->%d min_rel %.2f->%.2f",
                sq.id, old_top_k, new_top_k, old_min_rel, new_min_rel,
            )

    from app.agent.plan_executor import execute_plan  # noqa: PLC0415
    await execute_plan(plan, deps, parallel=True, overall_timeout_s=12.0)

    # Emit a Phase 4 SSE event so the UI can show revise happened.
    revised_ok = sum(1 for sq in plan.sub_queries if sq.id in failing_ids and sq.outcome == "ok")
    if emit is not None:
        try:
            await emit(
                "__agentic_revise_attempted__:"
                + json.dumps({
                    "type": "agentic_revise_attempted",
                    "revised_sq_count": len(failing_ids),
                    "post_revise_ok_count": revised_ok,
                })
            )
        except Exception:
            logger.debug(
                "_revise_failing_subqueries: SSE emit failed (non-fatal)", exc_info=True
            )

    return plan


async def _escalate_continued_empty(
    query: str,
    deps: AgentDeps,
    *,
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> DecompositionPlan | None:
    """Build and execute a broadened single-shot escalation plan.

    Phase 4.C — fires when the post_decomposition branch is
    'escalate_continued_empty' (prior turn was also empty).  Constructs a
    synthetic DecompositionPlan with ONE document_passage_search sub-query
    using no filters and top_k=20, executes it, and returns the plan if any
    evidence was found.  Returns None if execution is still empty so the
    orchestrator falls through to the linear refusal path.

    This is a "one-shot escalation" — the D5 revise budget is NOT consumed
    (escalation is decomposition-time, not binding-time).

    Does NOT raise — any exception is caught, logged, and None is returned.

    Args:
        query:  Effective query string (post-anaphora rewrite if applicable).
        deps:   AgentDeps for execute_plan.
        emit:   Optional SSE emit callback.

    Returns:
        Executed DecompositionPlan if >=1 sub-query returned ok, else None.
    """
    from app.agent.plan_executor import execute_plan  # noqa: PLC0415
    from app.models.decomposition import (  # noqa: PLC0415
        DecompositionPlan,
        DocumentPassageSearchInput,
        SubQueryDocumentPassageSearch,
    )

    try:
        if emit is not None:
            with contextlib.suppress(Exception):
                await emit("Broadening search for continued empty result…")

        escalation_sq = SubQueryDocumentPassageSearch(
            id="sq-escalation-1",
            sub_query_class="document_passage_search",
            input=DocumentPassageSearchInput(
                query_text=query,
                top_k=20,
                min_relevance=0.3,
                document_filter=None,  # no filter — maximum breadth
            ),
            latency_budget_s=5.0,
            outcome="pending",
        )
        escalation_plan = DecompositionPlan(
            trigger="continued_empty_escalation",
            sub_queries=[escalation_sq],
        )

        await execute_plan(escalation_plan, deps, parallel=False, overall_timeout_s=8.0)

        ok_count = sum(1 for sq in escalation_plan.sub_queries if sq.outcome == "ok")
        logger.info(
            "_escalate_continued_empty: escalation executed ok_count=%d/%d",
            ok_count,
            len(escalation_plan.sub_queries),
        )

        if ok_count == 0:
            if emit is not None:
                with contextlib.suppress(Exception):
                    await emit(
                        "__agentic_escalation_empty__:"
                        + json.dumps({"type": "agentic_escalation_empty"})
                    )
            return None

        return escalation_plan

    except Exception:
        logger.warning(
            "_escalate_continued_empty: escalation failed (non-fatal), "
            "falling through to linear path",
            exc_info=True,
        )
        return None
