"""pipeline/decomposition.py — Phase 1+2 A.2 decomposition helpers.

Extracted from ``app.agent.orchestrator`` during the Wave 2.C refactor.
The orchestrator delegates to these helpers; the public entry point
``run_deterministic_rag`` stays in ``app.agent.orchestrator``.

Functions owned here
--------------------
_is_agentic_retrieval_enabled   workspace feature-flag check (Phase 1.C-ii(a))
_format_agentic_evidence_block  evidence block formatter (Phase 1.C-ii(b))
_maybe_run_agentic_decomposition agentic decomposition hook (Phase 1.C-ii(a))
_maybe_read_and_resolve_anaphora anaphora resolution + state read (Phase 2.C)
_maybe_write_conversation_state conversation state write (Phase 2.C)

Circular-import note
--------------------
``_maybe_run_agentic_decomposition`` calls three helpers that live in
``pipeline/branching``.  Those imports are deferred to the function body
to avoid a circular import at module load time.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.agent.deps import AgentDeps
from app.agent.pipeline.branching import (
    _decide_post_retrieval,
    _emit_agentic_decision,
    _record_decision,
)

if TYPE_CHECKING:
    from app.models.decomposition import DecompositionPlan

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Track A.2 Phase 1.C-ii(a) — workspace feature-flag helper
# ---------------------------------------------------------------------------


async def _is_agentic_retrieval_enabled(deps: AgentDeps) -> bool:
    """Read silver.workspaces.agentic_retrieval_enabled for the current workspace.

    Returns False on:
      - deps.workspace_id is None (single-tenant / Dagster path)
      - Column doesn't exist (staging without migration 2026_05_12_120000 applied)
      - Any DB error (defensive — agentic path is opportunistic)
    """
    if not deps.workspace_id:
        return False
    try:
        async with deps.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT agentic_retrieval_enabled "
                "FROM silver.workspaces "
                "WHERE workspace_id = $1::uuid",
                str(deps.workspace_id),
            )
            return bool(row and row["agentic_retrieval_enabled"])
    except Exception:  # column missing / DB unavailable / etc
        logger.debug("_is_agentic_retrieval_enabled: defensive False", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Track A.2 Phase 1.C-ii(b) — evidence block formatter
# ---------------------------------------------------------------------------


def _format_agentic_evidence_block(plan: DecompositionPlan | None) -> str:
    """Format completed sub-queries as a synthesis-context evidence block.

    Returns an empty string when plan is None, has no sub-queries, or none of
    its sub-queries reached outcome='ok'.  Otherwise returns a multi-line block
    tagged "## Agentic decomposition evidence" so the LLM can distinguish typed
    sub-query results from the deterministic fan-out evidence, and so §04i
    Citation completeness validation can trace each claim back to its
    agentic-path source_chunk_id via the ``src=`` annotations.

    Sub-queries with outcome != 'ok' are silently omitted — the LLM does not
    need to see failed sub-queries in the synthesis context.

    Per-class formatting mirrors D2's typed output shapes from
    ``app.models.decomposition``:

    * factual_lookup         — value + source_chunk_id
    * entity_traversal       — path summaries (truncated at 3 hops + ellipsis)
    * spatial_filter         — count + up to 5 example row_ids
    * document_passage_search — passages count + up to 5 passage details
    * numerical_aggregation  — result + computation_sql SQL trail

    Ordering is by sub-query id (ASCII sort) for deterministic output.
    """
    if plan is None or not plan.sub_queries:
        return ""

    lines: list[str] = []

    ok_sub_queries = sorted(
        [sq for sq in plan.sub_queries if sq.outcome == "ok"],
        key=lambda sq: sq.id,
    )
    if not ok_sub_queries:
        return ""

    lines.append("## Agentic decomposition evidence")

    for sq in ok_sub_queries:
        sq_id = sq.id
        result = sq.result  # typed output; guaranteed non-None when outcome='ok'
        if result is None:
            # Defensive: outcome='ok' but result is None — skip rather than crash.
            continue

        if sq.sub_query_class == "factual_lookup":
            lines.append(
                f"[agentic-{sq_id} factual_lookup"
                f" table={sq.input.table}"
                f" entity={sq.input.entity_id}]"
                f" value={result.value}"
                f"  src={result.source_chunk_id}"
            )

        elif sq.sub_query_class == "entity_traversal":
            for path in result.paths:
                # Build human-readable "NodeA -[EDGE]-> NodeB -[EDGE]-> NodeC" string.
                # Truncate at 3 hops (4 nodes) + ellipsis so the context block stays compact.
                nodes = path.nodes
                edges = path.edges
                MAX_DISPLAY_HOPS = 3
                if len(edges) > MAX_DISPLAY_HOPS:
                    display_nodes = nodes[: MAX_DISPLAY_HOPS + 1]
                    display_edges = edges[:MAX_DISPLAY_HOPS]
                    ellipsis_suffix = " -> ..."
                else:
                    display_nodes = nodes
                    display_edges = edges
                    ellipsis_suffix = ""

                parts: list[str] = []
                for i, node in enumerate(display_nodes):
                    node_label = node.properties.get("name") or node.label
                    parts.append(str(node_label))
                    if i < len(display_edges):
                        parts.append(f"-[{display_edges[i].label}]->")
                path_summary = " ".join(parts) + ellipsis_suffix

                lines.append(
                    f"[agentic-{sq_id} entity_traversal"
                    f" start={sq.input.start_entity}]"
                    f" {path_summary}"
                    f"  src={result.source_chunk_id}"
                )

        elif sq.sub_query_class == "spatial_filter":
            lines.append(
                f"[agentic-{sq_id} spatial_filter"
                f" predicate={sq.input.predicate}"
                f" table={sq.input.target_table}]"
                f" count={result.result_count}"
                f"  src={result.source_chunk_id}"
            )
            for row in result.matching_rows[:5]:
                row_id = row.get("id") or row.get("collar_id") or row.get("pk") or row
                lines.append(f"  · row_id={row_id}")

        elif sq.sub_query_class == "document_passage_search":
            n_passages = len(result.passages)
            lines.append(
                f"[agentic-{sq_id} document_passage_search]"
                f" passages={n_passages}"
                f"  src={result.source_chunk_id}"
            )
            for passage in result.passages[:5]:
                text_preview = passage.text[:200].replace("\n", " ")
                lines.append(
                    f"  · pid={passage.passage_id}"
                    f" doc={passage.document_id}"
                    f" p={passage.page_number}"
                    f" rel={passage.relevance:.2f}"
                    f" text={text_preview}"
                )

        elif sq.sub_query_class == "numerical_aggregation":
            lines.append(
                f"[agentic-{sq_id} numerical_aggregation"
                f" op={sq.input.operation}"
                f" table={sq.input.target_table}.{sq.input.target_column}]"
                f" result={result.result}"
                f"  src={result.source_chunk_id}"
            )
            lines.append(f"  -- computation: {result.computation_sql}")

    lines.append("")  # trailing blank line
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Track A.2 Phase 1.C-ii(a) — agentic decomposition entry point
# ---------------------------------------------------------------------------


async def _maybe_run_agentic_decomposition(
    query: str,
    categories: dict[str, bool],
    deps: AgentDeps,
    *,
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> DecompositionPlan | None:
    """Conditionally decompose and execute a compound query via the A.2 planner.

    This function is the Phase 1.C-ii(a) orchestrator integration hook.  It
    fires after ``_classify_query`` + the LLM-classifier fallback have settled
    ``categories``, but BEFORE the existing tool-dispatch logic runs.

    Behaviour:
      1. Checks the workspace feature flag via ``_is_agentic_retrieval_enabled``.
         Returns None immediately if the flag is off (default for all workspaces
         until D8 default-on threshold is reached).
      2. Derives ``classifier_categories`` from truthy keys in ``categories`` (the
         spec vocabulary: spatial, documents, graph, factual, computation, viz).
      3. Calls ``detect_decomposition_trigger`` with ``ner_entities=[]`` (Phase
         1.C-ii(b) wires real NER; empty list means multi_entity_ner never
         fires here) and ``retrieval_result_count=None`` (hook fires BEFORE
         retrieval, so continued_empty_escalation never fires here).
      4. If no trigger fires, returns None — the existing §04h path continues.
      5. If a trigger fires, calls ``decompose_query`` (cap max_sub_queries=5
         per D4) then ``execute_plan`` in parallel with overall_timeout_s=12.0.
      6. Logs trigger + outcome counts; calls ``emit`` so the SSE stream surfaces
         the agentic path to the frontend.
      7. Returns the executed DecompositionPlan on success, None on any error
         (entire block is wrapped in a defensive try/except — a bug here must
         NEVER crash the orchestrator).

    The returned plan is currently observable but NOT yet merged into the
    synthesis context.  Phase 1.C-ii(b) merges it.

    Per §04i: the existing hallucination prevention layers remain active on the
    existing path.  The plan results do not bypass any §04i guard.
    """
    # Step 1 — workspace feature-flag check.
    try:
        enabled = await _is_agentic_retrieval_enabled(deps)
    except Exception:
        logger.warning(
            "_maybe_run_agentic_decomposition: flag check raised (defensive skip)",
            exc_info=True,
        )
        return None

    if not enabled:
        return None

    try:
        # Step 2 — derive classifier_categories list from truthy dict keys.
        # The spec vocabulary for the existing classifier is these six keys.
        # "classifier_fallback" is an internal routing sentinel — exclude it.
        _SPEC_CATEGORIES = frozenset(
            {"spatial", "documents", "graph", "factual", "computation", "viz"}
        )
        classifier_categories: list[str] = [
            k for k, v in categories.items() if v and k in _SPEC_CATEGORIES
        ]

        # Step 3 — trigger detection.
        # ner_entities=[] → multi_entity_ner never fires (Phase 1.C-ii(b) wires NER).
        # retrieval_result_count=None → continued_empty_escalation never fires
        #   (hook runs BEFORE retrieval; a future refinement re-calls this AFTER).
        from app.agent.decomposer import decompose_query, detect_decomposition_trigger  # noqa: PLC0415
        from app.agent.plan_executor import execute_plan  # noqa: PLC0415

        trigger = detect_decomposition_trigger(
            query_text=query,
            classifier_categories=classifier_categories,
            ner_entities=[],
            retrieval_result_count=None,
        )

        # Step 4 — no trigger → continue with existing path.
        if trigger is None:
            return None

        # Step 5 — trigger fired: decompose then execute.
        if emit is not None:
            await emit("Decomposing compound query…")

        plan = decompose_query(
            query_text=query,
            trigger=trigger,
            classifier_categories=classifier_categories,
            ner_entities=[],
            max_sub_queries=5,
        )

        logger.info(
            "run_deterministic_rag: agentic decomposition trigger=%s sub_queries=%d",
            trigger,
            len(plan.sub_queries),
        )

        await execute_plan(plan, deps, parallel=True, overall_timeout_s=12.0)

        # Step 6 — log per-sub-query outcome counts.
        from collections import Counter  # noqa: PLC0415

        outcome_counts = Counter(sq.outcome for sq in plan.sub_queries)
        logger.info(
            "run_deterministic_rag: agentic plan outcomes=%s",
            dict(outcome_counts),
        )

        # ── Track A.2 Phase 3 — D4 Decision point 2: post_retrieval ──────
        # Fires immediately after execute_plan returns with all sub-query
        # outcomes set.  Records to plan.decisions and emits SSE event.
        # Branching helpers imported at module top — branching never imports
        # decomposition, so there's no actual cycle to avoid (Wave 2.C
        # senior-reviewer issue #2).
        _d4_post_ret_branch = _decide_post_retrieval(plan)
        _ok_count = sum(1 for sq in plan.sub_queries if sq.outcome == "ok")
        _record_decision(
            plan,
            "post_retrieval",
            _d4_post_ret_branch,
            rationale=f"ok={_ok_count} total={len(plan.sub_queries)} outcomes={dict(outcome_counts)}",
        )
        await _emit_agentic_decision(
            emit,
            "post_retrieval",
            _d4_post_ret_branch,
            rationale=f"ok={_ok_count}/{len(plan.sub_queries)}",
        )
        logger.info(
            "_maybe_run_agentic_decomposition: D4 post_retrieval branch=%s ok=%d/%d",
            _d4_post_ret_branch,
            _ok_count,
            len(plan.sub_queries),
        )

        # refuse_insufficient: every sub_query failed — return None so the
        # orchestrator falls through to the §04h linear refusal path.
        if _d4_post_ret_branch == "refuse_insufficient":
            logger.info(
                "_maybe_run_agentic_decomposition: D4 refuse_insufficient — "
                "returning None so orchestrator routes to refusal"
            )
            return None

        return plan

    except Exception:
        logger.warning(
            "_maybe_run_agentic_decomposition: defensive skip after exception",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Track A.2 Phase 2.C — conversation state helpers
# ---------------------------------------------------------------------------


async def _maybe_read_and_resolve_anaphora(
    query: str,
    deps: AgentDeps,
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, object]:
    """Read typed conversation state and rewrite anaphoric query references.

    Gated on BOTH:
      (a) silver.workspaces.agentic_retrieval_enabled == True for the workspace
      (b) deps.conversation_id is not None

    When either condition is False this is a pure no-op that returns the
    original query unchanged and None for the state.

    When both are True:
      1. Reads chat_conversations.state_json via read_conversation_state().
      2. If a ConversationState is found, runs resolve_anaphora(query, state).
      3. If was_rewritten is True, emits an SSE event of kind
         ``agentic_anaphora_resolved`` with the original/rewritten query and
         the resolved entity IDs.
      4. Returns (rewritten_query, state) for downstream use.

    Failures at any step are swallowed: state read failure → returns original
    query + None.  The request is never broken by state read errors.

    Args:
        query:   Raw user query string from the request.
        deps:    AgentDeps; must have pg_pool and conversation_id populated.
        emit:    Optional async callback that accepts a structured string; used
                 to push SSE events back to the frontend.  None = no-op.

    Returns:
        (effective_query, prior_state)
        effective_query — possibly rewritten query for downstream classification.
        prior_state     — ConversationState from this turn's prior state, or None.
    """
    if deps.conversation_id is None:
        return query, None

    try:
        enabled = await _is_agentic_retrieval_enabled(deps)
    except Exception:
        logger.debug(
            "_maybe_read_and_resolve_anaphora: flag check failed (defensive skip)",
            exc_info=True,
        )
        return query, None

    if not enabled:
        return query, None

    from app.agent.anaphora import resolve_anaphora  # noqa: PLC0415
    from app.services.conversation_state_store import read_conversation_state  # noqa: PLC0415

    # ── Read prior state ────────────────────────────────────────────────────
    prior_state = None
    try:
        prior_state = await read_conversation_state(deps.pg_pool, deps.conversation_id)
    except Exception:
        logger.warning(
            "_maybe_read_and_resolve_anaphora: state read failed (non-fatal) "
            "conversation_id=%s",
            deps.conversation_id,
            exc_info=True,
        )
        return query, None

    if prior_state is None:
        # Fresh conversation or no prior agentic state — nothing to resolve.
        return query, None

    # ── Anaphora resolution ─────────────────────────────────────────────────
    try:
        rewritten_query, resolved_entity_ids, was_rewritten = resolve_anaphora(
            query, prior_state
        )
    except Exception:
        logger.warning(
            "_maybe_read_and_resolve_anaphora: anaphora resolution failed (non-fatal)",
            exc_info=True,
        )
        return query, prior_state

    if was_rewritten:
        logger.info(
            "_maybe_read_and_resolve_anaphora: anaphora resolved "
            "conversation_id=%s resolved_entity_ids=%s",
            deps.conversation_id,
            resolved_entity_ids,
        )
        if emit is not None:
            try:
                import json as _json  # noqa: PLC0415
                await emit(
                    "__agentic_anaphora_resolved__:"
                    + _json.dumps({
                        "type": "agentic_anaphora_resolved",
                        "original_query": query,
                        "rewritten_query": rewritten_query,
                        "resolved_entity_ids": resolved_entity_ids,
                    })
                )
            except Exception:
                logger.debug(
                    "_maybe_read_and_resolve_anaphora: emit failed (non-fatal)",
                    exc_info=True,
                )
        return rewritten_query, prior_state

    return query, prior_state


async def _maybe_write_conversation_state(
    deps: AgentDeps,
    *,
    last_query_class: str | None,
    entity_focus: list[str],
    spatial_focus: dict | None,
    temporal_focus: tuple | None,
    agentic_plan: DecompositionPlan | None,
) -> None:
    """Persist a fresh ConversationState after the answer is finalised.

    Gated on BOTH:
      (a) silver.workspaces.agentic_retrieval_enabled == True for the workspace
      (b) deps.conversation_id is not None

    When either condition is False this is a pure no-op.

    Builds a ConversationState from the current turn's data and writes it to
    chat_conversations.state_json via update_conversation_state().

    Failures are swallowed — state write never breaks response delivery.

    Args:
        deps:              AgentDeps with pg_pool + conversation_id.
        last_query_class:  Classifier verdict for this turn (may be None).
        entity_focus:      Entity IDs surfaced by this turn's retrieval.
        spatial_focus:     Spatial filter applied this turn (bbox or centroid).
        temporal_focus:    Temporal filter applied this turn (start, end dates).
        agentic_plan:      Completed DecompositionPlan if agentic path fired.
    """
    if deps.conversation_id is None:
        return

    try:
        enabled = await _is_agentic_retrieval_enabled(deps)
    except Exception:
        logger.debug(
            "_maybe_write_conversation_state: flag check failed (defensive skip)",
            exc_info=True,
        )
        return

    if not enabled:
        return

    from app.models.conversation_state import ConversationState  # noqa: PLC0415
    from app.services.conversation_state_store import update_conversation_state  # noqa: PLC0415

    # ── Build new state from this turn ──────────────────────────────────────
    _last_plan_json: dict | None = None
    if agentic_plan is not None:
        try:
            from app.models.decomposition import serialize_plan_for_jsonb  # noqa: PLC0415
            _last_plan_json = serialize_plan_for_jsonb(agentic_plan)
        except Exception:
            logger.debug(
                "_maybe_write_conversation_state: plan serialisation failed (non-fatal)",
                exc_info=True,
            )

    try:
        new_state = ConversationState(
            schema_version="1",
            last_query_class=last_query_class,
            entity_focus=entity_focus,
            spatial_focus=spatial_focus,
            temporal_focus=temporal_focus,
            pending_followup=None,  # Phase 4 will populate this
            last_plan_json=_last_plan_json,
        )
    except Exception:
        logger.warning(
            "_maybe_write_conversation_state: ConversationState construction failed "
            "(non-fatal) conversation_id=%s",
            deps.conversation_id,
            exc_info=True,
        )
        return

    # ── Write to DB ─────────────────────────────────────────────────────────
    try:
        ok = await update_conversation_state(
            deps.pg_pool, deps.conversation_id, new_state
        )
        if ok:
            logger.debug(
                "_maybe_write_conversation_state: persisted conversation_id=%s "
                "last_query_class=%s entity_focus_count=%d",
                deps.conversation_id,
                last_query_class,
                len(entity_focus),
            )
        else:
            logger.debug(
                "_maybe_write_conversation_state: update returned False "
                "(conversation_id=%s — likely missing row or no-op)",
                deps.conversation_id,
            )
    except Exception:
        logger.warning(
            "_maybe_write_conversation_state: DB write failed (non-fatal) "
            "conversation_id=%s",
            deps.conversation_id,
            exc_info=True,
        )
