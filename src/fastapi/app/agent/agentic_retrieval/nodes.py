"""LangGraph node implementations — Phase 2 / Step 2.3.

Six nodes wire the pipeline ``classify → route → execute → assemble →
validate → demote``. Each takes the current :class:`AgenticRetrievalState`
and returns a partial-update dict that LangGraph merges back.

The nodes are intentionally thin — heavy lifting lives in the existing
tool layer (``app.agent.tools``), the existing assembler / validators
(``app.agent.response_assembler`` + ``app.agent.hallucination``), and the
existing confidence demoter (``app.agent.confidence_computer``). The
agentic-retrieval pipeline orchestrates those building blocks rather than
re-implementing them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import Any

import asyncpg

from app.agent.agentic_retrieval.context_envelope import (
    apply_envelope_overrides,
    unspecified_field_descriptions,
)
from app.agent.agentic_retrieval.intent_classifier import classify_intent
from app.agent.agentic_retrieval.preprocessor import preprocess_envelope
from app.agent.agentic_retrieval.retrieval_profile import (
    RetrievalProfile,
    profile_for_intent,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

#: Bumped when the chat path's prompt/graph shape changes materially,
#: so a usage.usage_events row can be attributed to a code version.
CHAT_USAGE_AGENT_VERSION = "agentic-v2"


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resolve (Plan §3e multi-turn — runs BEFORE classify when flag is on)
# ---------------------------------------------------------------------------


async def resolve_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Plan §3e — rewrite state.query using conversation history.

    Runs ahead of the 6-intent classifier so the classifier sees the
    expanded query ("what are PLS-22-08's top assays?") instead of the
    pronoun-laden original ("what are ITS top assays?").

    When ``settings.MULTI_TURN_RESOLUTION_ENABLED`` is False (default is
    True since 2026-08-14 — the resolver is pure/heuristic; the flag is
    the escape hatch), this node returns ``{}`` — no rewrite, no state
    change. Same shape as the repair-loop shadow node.

    Best-effort: any exception inside the resolver logs but does NOT
    block the answer path — the classifier just sees the un-rewritten
    query (which is what it would have seen before §3e).

    See docs/architecture/multi_turn_resolution_spec.md §6 for the
    end-to-end contract; this node implements §6.3.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.MULTI_TURN_RESOLUTION_ENABLED:
        return {}

    if not state.history:
        return {}

    try:
        from app.agent.multi_turn_resolver import (  # noqa: PLC0415
            REWRITE_MIN_CONFIDENCE,
            resolve_multi_turn,
        )

        resolved = resolve_multi_turn(state.query, list(state.history))

        # Everything goes through the returned update dict. This node used
        # to ALSO assign state.resolution_trace in place "as the spec
        # wants"; LangGraph rebuilds the state from channels for the next
        # node, so an in-place write is discarded (audit AGT-5).
        trace = [
            {
                "kind": s.kind,
                "original_phrase": s.original_phrase,
                "resolved_to": s.resolved_to,
                "source_turn_index": s.source_turn_index,
                "confidence": s.confidence,
            }
            for s in resolved.resolution_trace
        ]

        if not resolved.made_changes:
            # Stamp the confidence even when no substitution happened —
            # a fully-resolvable query is a positive signal, and a low one
            # (an unresolvable or ambiguous pronoun) is worth recording.
            return {"resolution_confidence": resolved.overall_confidence}

        if resolved.overall_confidence < REWRITE_MIN_CONFIDENCE:
            # Audit AGT-1: a low-confidence rewrite is a guess, and the
            # rewritten string replaces state.query for the classifier,
            # the hole-ID pre-pass, retrieval and persistence. Keep the
            # user's words; the log records what was withheld.
            logger.info(
                "agentic_retrieval.resolve: rewrite withheld "
                "(steps=%d, confidence=%.2f < %.2f)",
                len(trace), resolved.overall_confidence, REWRITE_MIN_CONFIDENCE,
            )
            return {"resolution_confidence": resolved.overall_confidence}

        logger.info(
            "agentic_retrieval.resolve: rewrote query "
            "(steps=%d, confidence=%.2f)",
            len(trace),
            resolved.overall_confidence,
        )

        return {
            "query": resolved.rewritten_query,
            "query_original": state.query,
            "resolution_trace": trace,
            "resolution_confidence": resolved.overall_confidence,
        }
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "agentic_retrieval.resolve: failed (non-fatal, falling back "
            "to un-rewritten query)"
        )
        return {}


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


async def classify_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Run the 6-intent classifier; populate ``intent`` + ``intent_result``."""
    if state.status_callback is not None:
        try:
            await state.status_callback("Classifying query…")
        except Exception:  # pragma: no cover — status is a UX affordance
            logger.debug("agentic_retrieval.classify: status_callback raised", exc_info=True)
    openai_client = getattr(state.deps, "openai_http_client", None)
    result = await classify_intent(
        state.query, openai_http_client=openai_client, deps=state.deps,
    )
    logger.info(
        "agentic_retrieval.classify: intent=%s confidence=%.2f used_llm=%s triggers=%s",
        result.intent,
        result.confidence,
        result.used_llm_fallback,
        result.matched_triggers[:5],
    )
    return {
        "intent": result.intent,
        "intent_result": result,
        # The classifier escalates to the LLM on ambiguous queries
        # (result.used_llm_fallback), and those tokens are billed.
        **_fold_token_usage(state),
    }


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------


async def route_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Select the retrieval profile, applying envelope overrides.

    Step 2.4 layered onto Step 2.3:
      1. Take the classifier's intent
      2. Apply the envelope's routing-override table (e.g. demote
         decision_support → synthesis when no decision context was supplied)
      3. Look up the retrieval profile for the *effective* intent
    """
    assert state.intent is not None, "classify_node must run before route_node"
    regulatory = bool(
        state.intent_result and state.intent_result.regulatory_touch
    )

    decision = apply_envelope_overrides(state.intent, state.context_envelope)
    effective = decision.effective_intent
    if effective != state.intent:
        logger.info(
            "agentic_retrieval.route: envelope override %s → %s (%s)",
            state.intent,
            effective,
            decision.override_reason,
        )

    profile = profile_for_intent(effective, regulatory_touch=regulatory)

    # Phase 3 / Step 3.1 — pre-process envelope into retrieval filters.
    filters = preprocess_envelope(state.context_envelope)
    logger.info(
        "agentic_retrieval.route: intent=%s effective_intent=%s primary_tools=%s "
        "adversarial=%s conflict_detection=%s require_regulatory=%s mode=%s "
        "crs_epsg=%s allowed_data_sources=%s",
        state.intent,
        effective,
        profile.primary_tools,
        profile.adversarial_pass_enabled,
        profile.conflict_detection_enabled,
        profile.require_regulatory_constraints,
        filters.mode,
        filters.crs_epsg,
        sorted(filters.allowed_data_sources) if filters.allowed_data_sources else "(all)",
    )
    return {
        "retrieval_profile": profile,
        "retrieval_filters": filters,
        "effective_intent": effective,
        "envelope_override_reason": decision.override_reason,
        "envelope_notes": list(decision.notes),
    }


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def _hole_ids_from_query(query: str) -> list[str]:
    """Return the (up-to-3) hole IDs named in *query*. Wrapper around
    :func:`app.agent.viz_builder.extract_hole_ids` so the import stays
    lazy + a failure here can't sink the whole graph."""
    if not query:
        return []
    try:
        from app.agent.viz_builder import extract_hole_ids  # noqa: PLC0415
    except Exception:  # pragma: no cover — defensive
        logger.exception("agentic_retrieval.execute: extract_hole_ids import failed")
        return []
    try:
        found = extract_hole_ids(query)
    except Exception:  # pragma: no cover — defensive
        logger.exception("agentic_retrieval.execute: extract_hole_ids failed")
        return []
    # "PLS-22-08" also yields its numeric tail "22-08" (the Cameco-shape
    # pattern). Two IDs for one hole meant a second collar lookup and, for
    # the assay filter, a second hole (audit AGT-1 / AGT-15). Drop any ID
    # that is only the separator-delimited tail of a longer one found in
    # the same question ("BH-1" next to "BH-12" is kept: not a tail).
    upper = [h.upper() for h in found]

    def _is_tail_of_another(u: str) -> bool:
        return any(
            len(other) > len(u)
            and other.endswith(u)
            and not other[-len(u) - 1].isalnum()
            for other in upper
        )

    distinct = [
        h for h, u in zip(found, upper, strict=True) if not _is_tail_of_another(u)
    ]
    return list(dict.fromkeys(distinct))[:3]


# Question / command / generic words that are TitleCase at a sentence start but
# are never entity names. Lowercased for comparison.
_QUERY_STOPWORDS: frozenset[str] = frozenset({
    "what", "which", "where", "when", "who", "how", "why", "is", "are", "was",
    "were", "the", "a", "an", "tell", "me", "about", "show", "list", "find",
    "give", "does", "do", "did", "can", "could", "would", "should", "please",
    "and", "or", "of", "in", "on", "for", "to", "with", "from", "by", "at",
    "this", "that", "these", "those", "there", "here", "project", "data",
    "report", "summary", "between", "any", "all",
})

# Runs of 1–4 TitleCase words ("Triple R Deposit", "Athabasca Group"). Single
# capital letters are allowed WITHIN a run ("Triple R Deposit"); standalone
# single-char candidates are dropped below by the len>=2 guard.
_TITLECASE_RUN_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,3})\b")
# Anything in single/double quotes (2–80 chars).
_QUOTED_ENTITY_RE = re.compile(r"""['"]([^'"]{2,80})['"]""")


def _entity_names_from_query(query: str) -> list[str]:
    """Best-effort entity-name extraction for ``traverse_knowledge_graph``.

    Lightweight (no NER model): quoted strings first, then runs of TitleCase
    words minus question/stopwords. ``traverse_knowledge_graph`` fuzzy-matches
    (exact → CONTAINS) and returns an empty result gracefully on a miss, so a
    noisy extraction is harmless — it yields an empty graph result, never a
    wrong one. Returns up to 3 candidates, longest (most specific) first.

    Audit 2026-06-28: before this, the dispatcher unconditionally skipped
    traverse_knowledge_graph ("NER unwired"), so Neo4j was never consulted in
    agentic chat even though three intent profiles list it as a primary tool.
    """
    if not query:
        return []
    out: list[str] = []
    for cand in _QUOTED_ENTITY_RE.findall(query):
        cand = cand.strip()
        if cand and cand.lower() not in _QUERY_STOPWORDS:
            out.append(cand)
    for run in _TITLECASE_RUN_RE.findall(query):
        words = [w for w in run.split() if w.lower() not in _QUERY_STOPWORDS]
        if not words:
            continue
        cleaned = " ".join(words)
        # Drop single-char standalone candidates ("R", "I") — too noisy to be
        # an entity on their own; multi-word runs that contain them are kept.
        if len(cleaned) >= 2 and cleaned not in out:
            out.append(cleaned)
    deduped = list(dict.fromkeys(out))
    deduped.sort(key=len, reverse=True)
    return deduped[:3]


async def _call_tool_safely(tool_name: str, query: str, deps: Any) -> Any | None:
    """Dispatch into ``app.agent.tools`` using the real per-tool signatures.

    The legacy tool layer's functions were authored as Pydantic-AI
    ``@geo_agent.tool`` callables and therefore declare a
    ``RunContext[AgentDeps]`` first positional parameter:

      - ``search_documents(ctx, query_text, project_id, ...)``
      - ``query_spatial_collars(ctx, project_id, ...)``
      - ``query_assay_data(ctx, project_id, ...)``
      - ``query_downhole_logs(ctx, project_id, hole_id)``  — hole_id required
      - ``traverse_knowledge_graph(ctx, entity_name, project_id, ...)``
      - ``query_project_overview(ctx, project_id)``

    We build the same ``ToolContext`` shim the deterministic orchestrator
    uses (``app.agent.deps.ToolContext``) so the tools can read their
    asyncpg / Qdrant / Neo4j clients off ``ctx.deps`` without involving
    Pydantic-AI's runtime.

    The ADR-0007 PR-1 chat-card tools (``query_project_summary`` /
    ``query_coverage_gap``) are *not* RunContext-based — they take
    ``(deps, workspace_id, project_id)`` directly and have their own
    dispatch branch below.

    ``query_downhole_logs`` and ``traverse_knowledge_graph`` need NER
    extraction of a hole_id / entity_name from the user query, which is
    Phase 2.3's "secondary" complexity we punted on. We skip them
    cleanly until the entity-extraction step lands.

    Failures (incl. skipped tools) return None so one bad tool doesn't
    sink the whole graph.
    """
    try:
        from app.agent import tools as _t  # noqa: PLC0415
    except Exception:
        logger.exception("agentic_retrieval.execute: tools import failed")
        return None

    fn = getattr(_t, tool_name, None)
    if fn is None:
        logger.warning("agentic_retrieval.execute: unknown tool %s — skipped", tool_name)
        return None

    project_id = getattr(deps, "project_id", None)
    if project_id is None:
        logger.warning(
            "agentic_retrieval.execute: deps.project_id missing — skipping %s",
            tool_name,
        )
        return None

    # Strip the agentic suffix we tag on for the adversarial pass so the
    # function lookup hits the real tool.
    real_name = "search_documents" if tool_name == "search_documents_adversarial" else tool_name

    # ADR-0007 PR-1 chat-card tools take ``(deps, workspace_id, project_id)``
    # directly — no RunContext wrapper. workspace_id is JWT-derived and
    # MUST be supplied; we used to silently fall back to the default
    # tenant when deps.workspace_id wasn't set, which the 2026-06-03 audit
    # established as a multi-tenant contamination bug. Now resolved via
    # WorkspaceContext.from_state which emits a metric on every fallback
    # (Phase 1 observe-only) and will hard-fail in Phase 2.
    from app.agent.workspace_context import WorkspaceContext  # noqa: PLC0415
    workspace_id = WorkspaceContext.from_state(
        deps, site="agentic_retrieval.execute_node.chat_cards",
    ).workspace_id

    # ToolContext is the same shim the deterministic orchestrator uses to
    # adapt AgentDeps into a RunContext-shaped object for the legacy tools.
    from app.agent.deps import ToolContext  # noqa: PLC0415
    ctx = ToolContext(deps)

    try:
        if real_name == "search_documents":
            # B3 identifier boost — exact-token identifiers (hole IDs,
            # sample IDs, commodity codes) retrieve better through the
            # sparse branch; widen its prefetch pool when one is present.
            from app.services.identifier_boost import detect_identifiers  # noqa: PLC0415
            _boost = detect_identifiers(query).boost_factor
            return await fn(ctx, query, project_id, sparse_boost_factor=_boost)
        if real_name == "query_assay_data":
            # Audit AGT-15 / RAG-6: this used to be (ctx, project_id) only,
            # so "average gold grade?" in a U+Au project got uranium
            # statistics and "top Au assays in PLS-22-08" got the whole
            # project. The commodity the question names now picks the
            # element key; none (or several) named -> per-element
            # summaries. Named holes filter the rows and the aggregates.
            commodities = _t.commodities_in_query(query)
            hole_ids = _hole_ids_from_query(query)
            return await fn(
                ctx, project_id,
                commodity=commodities[0] if len(commodities) == 1 else None,
                hole_ids=hole_ids or None,
            )
        if real_name in (
            "query_spatial_collars",
            "query_project_overview",
        ):
            return await fn(ctx, project_id)
        if real_name in ("query_project_summary", "query_coverage_gap"):
            return await fn(deps, workspace_id, project_id)
        if real_name == "query_stereonet":
            # ADR-0007 PR-2 — also takes (deps, workspace_id, project_id);
            # structure_filter stays None at the agentic-call layer (the
            # tool itself supports it for direct callers).
            return await fn(deps, workspace_id, project_id)
        if real_name == "query_drill_traces_3d":
            # ADR-0007 PR-4 — (deps, workspace_id, project_id, hole_id?).
            # hole_id comes from the same NER pass the hole-id pre-pass
            # uses; None for the project-wide variant.
            hole_ids = _hole_ids_from_query(query)
            return await fn(
                deps, workspace_id, project_id,
                hole_ids[0] if hole_ids else None,
            )
        if real_name == "search_public_geoscience":
            # Keyword-only after ctx, and takes no project_id at all: the
            # public-geoscience corpus is government-published data that is
            # not scoped to a workspace's projects.
            #
            # Filter hints (jurisdiction / canonical type / commodity) are
            # deliberately left None here so the tool searches all active
            # jurisdictions and all six canonical types. _classify_query
            # already extracts those hints for the deterministic
            # orchestrator, but they are not threaded through the agentic
            # planner's state — narrowing on hints this layer cannot see
            # would silently drop results, whereas the unfiltered call is
            # merely slower.
            return await fn(ctx, text_query=query)
        if real_name == "query_downhole_logs":
            # Hole-ID NER lands via extract_hole_ids (PR-3 / 2026-05-25).
            # When the query names a hole we route through; otherwise we
            # keep the historical skip behaviour so synthesis-style
            # secondary calls don't false-fire.
            hole_ids = _hole_ids_from_query(query)
            if not hole_ids:
                logger.info(
                    "agentic_retrieval.execute: %s no hole_id in query — skipped",
                    real_name,
                )
                return None
            return await fn(ctx, project_id, hole_ids[0])
        if real_name == "query_collar_details":
            # Always takes (deps, workspace_id, project_id, hole_id).
            # Caller (execute_node) handles the per-hole loop; this branch
            # only fires when a single specific hole is being dispatched.
            hole_ids = _hole_ids_from_query(query)
            if not hole_ids:
                logger.info(
                    "agentic_retrieval.execute: %s no hole_id in query — skipped",
                    real_name,
                )
                return None
            return await fn(deps, workspace_id, project_id, hole_ids[0])
        if real_name == "traverse_knowledge_graph":
            # Audit 2026-06-28: wire lightweight entity extraction so the
            # graph store is actually consulted when the query names an entity
            # (three intent profiles list this as a primary tool). The tool
            # fuzzy-matches and returns empty gracefully, so a missed/noisy
            # extraction is a clean no-op rather than a wrong answer.
            entity_names = _entity_names_from_query(query)
            if not entity_names:
                logger.info(
                    "agentic_retrieval.execute: %s no entity name extracted from "
                    "query — graph traversal skipped (no entity)",
                    real_name,
                )
                return None
            logger.info(
                "agentic_retrieval.execute: %s firing for entity=%r",
                real_name, entity_names[0],
            )
            return await fn(ctx, entity_names[0], project_id)
        if real_name == "query_spatial_geometry":
            # Plan §2g — wired call. Needs caller to supply geometry
            # via the spatial intent system (or future spec extractor).
            # Currently the orchestrator doesn't auto-trigger this; the
            # dispatch is here so a profile / classifier that ADDS
            # `query_spatial_geometry` to primary_tools can call into
            # the tool. The tool itself returns None without geometry,
            # so a profile-level enable without an intent extractor
            # is a clean no-op.
            try:
                from app.agent.tools_geospatial import query_spatial_geometry  # noqa: PLC0415
            except Exception:
                logger.exception(
                    "agentic_retrieval.execute: tools_geospatial import failed"
                )
                return None
            return await query_spatial_geometry(
                deps,
                workspace_id,
                project_id,
                query_text=query,
                # geometry_wkt is intentionally None — the tool returns
                # None when absent, which is the desired no-op until
                # the spec extractor lands.
            )
        # Unknown shape — best-effort guess with (ctx, query, project_id).
        return await fn(ctx, query, project_id)
    except Exception:
        logger.exception(
            "agentic_retrieval.execute: tool %s failed", real_name
        )
        return None


def _fold_token_usage(state: AgenticRetrievalState) -> dict[str, int]:
    """Fold THIS node's LLM token spend onto the run total.

    Every node that can reach the LLM returns ``**_fold_token_usage(state)``
    alongside its own update, so the totals accumulate across
    classify → assemble → repair.

    This exists because `llm_calls.get_run_token_usage()` cannot be read at
    the end of the run. LangGraph executes each node in its own
    `asyncio.Task`, and a Task is created with a COPY of the current
    context — so a `ContextVar.set()` inside assemble_node is invisible
    everywhere downstream. Verified 2026-08-21 with a two-node probe: node
    A set a contextvar to 41, node B read back the default. A
    `get_run_token_usage()` call in persist_node would have written a
    confident, permanent 0 into `silver.answer_runs.input_tokens`, which is
    worse than the NULL it replaced.

    Reading inside the node is safe for the same reason: the counters were
    reset to the parent's values when this node's Task was created, so what
    they hold now is exactly what this node spent.
    """
    from app.agent.llm_calls import get_run_token_usage  # noqa: PLC0415

    try:
        node_input, node_output = get_run_token_usage()
    except Exception:  # pragma: no cover — accounting must never break a run
        logger.debug("agentic_retrieval: token usage read failed", exc_info=True)
        return {}

    return {
        "llm_input_tokens": state.llm_input_tokens + int(node_input or 0),
        "llm_output_tokens": state.llm_output_tokens + int(node_output or 0),
    }


def _build_adversarial_query(query: str) -> str:
    """Rewrite a query to surface DISCONFIRMING evidence.

    Used by hypothesis-generation's adversarial pass. Implementation: a
    prefix that tells the dense / sparse retriever to look for chunks
    that argue against the leading interpretation. This is a *cheap
    approximation* of true adversarial retrieval — it uses the same
    corpus and same vectoriser, just biased prompt framing.
    """
    return (
        "Find evidence that CONTRADICTS or LIMITS the following geological "
        f"interpretation: {query}"
    )


#: search_documents failures that FAIL the query (RAG-12, GI-11): the
#: sparse leg is gone or Qdrant errored, and there is no dense-only
#: fallback by design. A timeout or an unloaded model is surfaced in
#: degraded_sources instead (and turns a Layer 1 refusal into a failure,
#: see assemble_node), since the next query may well succeed.
_HARD_RETRIEVAL_FAILURES = frozenset({"sparse_encoder_unavailable", "error"})


def _evidence_units(results: list[tuple[str, Any]]) -> int:
    """Pieces of evidence, not tool results (AGT-18): each document chunk
    and public-geoscience record counts once; a structured lookup counts
    once however many rows it returned."""
    units = 0
    for _name, result in results:
        chunks = getattr(result, "chunks", None)
        records = getattr(result, "records", None)
        if chunks is not None:
            units += len(chunks)
        elif records is not None:
            units += len(records)
        else:
            units += 1
    return units


async def execute_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Dispatch the profile's primary (and optionally secondary) tools.

    Phase 3 / Step 3.1: when ``state.retrieval_filters.allowed_data_sources``
    is non-empty, tools whose data-source surface doesn't intersect the
    allowed set are skipped. The skipped tools are logged so the lineage
    artifact can record what the user explicitly filtered out.
    """
    assert state.retrieval_profile is not None, (
        "route_node must run before execute_node"
    )
    if state.status_callback is not None:
        try:
            await state.status_callback("Querying PostGIS + Qdrant…")
        except Exception:  # pragma: no cover — status is a UX affordance
            logger.debug("agentic_retrieval.execute: status_callback raised", exc_info=True)
    profile: RetrievalProfile = state.retrieval_profile
    filters = state.retrieval_filters

    def _allowed(tool_name: str) -> bool:
        if filters is None:
            return True
        return filters.is_tool_allowed(tool_name)

    results: list[tuple[str, Any]] = []

    # Hole-ID pre-pass — if the user named a specific drill hole, look it
    # up directly via query_collar_details for every hole mentioned (up to
    # 3). This runs ALONGSIDE the profile's primary_tools so factual_lookup
    # ("tell me about hole 36-1085") returns a real cited answer instead
    # of falling through to search_documents alone.
    hole_ids = _hole_ids_from_query(state.query)

    # Plan §2c entity-resolver shadow pass — when
    # ENTITY_RESOLVER_SHADOW_ENABLED, look each extracted hole ID up
    # in silver.entity_aliases. Hits get logged; misses INSERT into
    # silver.alias_gaps so the SME review queue catches them.
    # Pure observability — does NOT modify the retrieval path. Stage 4
    # of ADR-0009 swaps the resolved canonical name into the query;
    # for now we just collect data on alias frequency / miss rate.
    if hole_ids:
        await _entity_resolver_shadow(state, hole_ids)
    if hole_ids and _allowed("query_collar_details"):
        try:
            from app.agent.tools import query_collar_details  # noqa: PLC0415
        except Exception:
            logger.exception(
                "agentic_retrieval.execute: query_collar_details import failed"
            )
        else:
            from app.agent.workspace_context import WorkspaceContext  # noqa: PLC0415
            workspace_id = WorkspaceContext.from_state(
                state.deps, site="agentic_retrieval.execute_node.collar_details",
            ).workspace_id
            project_id = getattr(state.deps, "project_id", None)
            if project_id is not None:
                # Independent per-hole lookups — gather instead of awaiting
                # serially. Each call is individually exception-guarded so a
                # single bad hole_id can't sink the others.
                async def _lookup_collar(hid: str) -> Any | None:
                    try:
                        return await query_collar_details(
                            state.deps, workspace_id, project_id, hid
                        )
                    except Exception:
                        logger.exception(
                            "agentic_retrieval.execute: query_collar_details failed"
                            " hole=%s",
                            hid,
                        )
                        return None

                collar_results = await asyncio.gather(
                    *(_lookup_collar(hid) for hid in hole_ids)
                )
                for result in collar_results:
                    if _worth_citing("query_collar_details", result):
                        results.append(("query_collar_details", result))
                logger.info(
                    "agentic_retrieval.execute: hole-id pre-pass yielded %d result(s)"
                    " for %d hole_id(s)",
                    sum(1 for n, _ in results if n == "query_collar_details"),
                    len(hole_ids),
                )

    # Primary pass — every primary tool is invoked once with the user query.
    # Perf audit 2026-08-15: primary tools are mutually independent (each
    # hits its own PostGIS/Qdrant/Neo4j path) and _call_tool_safely already
    # exception-guards every call, so gathering them is safe — no tool's
    # failure or slowness can affect another's result, and the appended
    # order below still matches profile.primary_tools regardless of
    # completion order (gather preserves input order).
    async def _dispatch_primary(tool_name: str) -> tuple[str, Any | None]:
        if not _allowed(tool_name):
            logger.info(
                "agentic_retrieval.execute: skipped %s (filtered out by data_sources)",
                tool_name,
            )
            return tool_name, None
        result = await _call_tool_safely(tool_name, state.query, state.deps)
        return tool_name, result

    # Audit RAG-12: a document search that FAILED (sparse encoder down,
    # Qdrant error, timeout) returns an empty result that _worth_citing
    # then drops, so the outage looked exactly like an empty corpus — a
    # false "no passages cleared the threshold" refusal, or an answer built
    # without documents and degraded_sources empty. Read the failure before
    # the drop.
    retrieval_failures: list[str] = []

    def _note_retrieval_failure(tool_name: str, result: Any) -> None:
        failure = getattr(result, "retrieval_failure", None)
        if not failure:
            return
        label = f"{getattr(result, 'data_source', 'Qdrant')} via {tool_name}"
        retrieval_failures.append(label)
        if failure in _HARD_RETRIEVAL_FAILURES:
            from app.agent.errors import RetrievalBackendUnavailable  # noqa: PLC0415

            logger.error(
                "agentic_retrieval.execute: %s — failing the query rather "
                "than answering without document retrieval (GI-11)", label,
            )
            raise RetrievalBackendUnavailable(failure)
        logger.warning(
            "agentic_retrieval.execute: %s — continuing, surfaced in "
            "degraded_sources", label,
        )

    primary_results = await asyncio.gather(
        *(_dispatch_primary(tool_name) for tool_name in profile.primary_tools)
    )
    for tool_name, result in primary_results:
        _note_retrieval_failure(tool_name, result)
        if _worth_citing(tool_name, result):
            results.append((tool_name, result))

    # Adversarial pass (hypothesis-generation only) — re-issue the
    # document-search tool against a disconfirming query framing.
    if profile.adversarial_pass_enabled and _allowed("search_documents_adversarial"):
        adversarial_query = _build_adversarial_query(state.query)
        result = await _call_tool_safely(
            "search_documents", adversarial_query, state.deps
        )
        _note_retrieval_failure("search_documents_adversarial", result)
        if result is not None:
            # The disconfirming framing re-retrieves much of the primary
            # pass's chunk set; drop duplicates so the context doesn't
            # double-count the same evidence under two citation ids.
            _prior_chunk_ids = {
                getattr(c, "chunk_id", None)
                for _, _r in results
                for c in (getattr(_r, "chunks", None) or ())
            }
            _adv_chunks = getattr(result, "chunks", None)
            if _adv_chunks:
                _fresh = [
                    c for c in _adv_chunks
                    if getattr(c, "chunk_id", None) not in _prior_chunk_ids
                ]
                if len(_fresh) != len(_adv_chunks):
                    result.chunks = _fresh
                    result.count = len(_fresh)
            # Checked here rather than at the `result is not None` guard
            # above, because the dedupe immediately preceding this can empty
            # a result that arrived non-empty.
            if _worth_citing("search_documents_adversarial", result):
                results.append(("search_documents_adversarial", result))
                logger.info(
                    "agentic_retrieval.execute: adversarial pass returned a result"
                )

    # Secondary tools — best-effort; failures don't block the pipeline.
    # They fire only when the primary pass yielded fewer than
    # _SECONDARY_COVERAGE_THRESHOLD pieces of EVIDENCE (audit AGT-18). This
    # used to be `len(results)` — one entry per TOOL — so factual_lookup,
    # whose only primary tool is search_documents, always had 1 < 3 and
    # always paid for search_public_geoscience, contrary to the profile's
    # "only when the internal corpus came back under threshold".
    _SECONDARY_COVERAGE_THRESHOLD = 3
    if (
        _evidence_units(results) < _SECONDARY_COVERAGE_THRESHOLD
        and profile.secondary_tools
    ):
        # Same independence argument as the primary pass — gather instead
        # of serial awaits.
        async def _dispatch_secondary(tool_name: str) -> tuple[str, Any | None]:
            if not _allowed(tool_name):
                return tool_name, None
            result = await _call_tool_safely(tool_name, state.query, state.deps)
            return tool_name, result

        secondary_results = await asyncio.gather(
            *(_dispatch_secondary(tool_name) for tool_name in profile.secondary_tools)
        )
        for tool_name, result in secondary_results:
            _note_retrieval_failure(tool_name, result)
            if _worth_citing(tool_name, result):
                results.append((tool_name, result))

    # ADR-0007 PR-2 — stereonet card. Trigger off explicit query keywords
    # rather than adding query_stereonet to a static profile (it's a
    # CPU-bound mplstereonet render — running it on every synthesis query
    # would waste worker time). Keywords are deliberately narrow.
    q_lower = (state.query or "").lower()
    _STEREONET_TRIGGERS = (
        "stereonet", "stereo net", "schmidt net", "wulff net",
        "pole to plane", "structural measurement",
    )
    if (
        any(t in q_lower for t in _STEREONET_TRIGGERS)
        and _allowed("query_stereonet")
        and not any(name == "query_stereonet" for name, _ in results)
    ):
        result = await _call_tool_safely("query_stereonet", state.query, state.deps)
        # count == 0 is what the tool returns on timeout, error or no data
        # (never None); an empty card is not worth rendering and must not
        # reach Layer 1 looking like a result (AGT-4).
        if result is not None and getattr(result, "count", 1):
            results.append(("query_stereonet", result))
            logger.info(
                "agentic_retrieval.execute: stereonet card triggered (keyword match)"
            )

    # ADR-0007 PR-4 — 3D drill-trace card. Same keyword-gated dispatch as
    # the stereonet card: we don't want to fire query_drill_traces_3d on
    # every synthesis query (it loads up to 200 collars + 1000 intervals
    # + 500 structures). Two trigger modes:
    #
    #   * If the query names a hole AND mentions "3d" or "trace",
    #     dispatch with that hole_id (single-hole view).
    #   * If the intent is synthesis / project_summary AND any drill-trace
    #     keyword fires, dispatch project-wide (hole_id=None).
    _DRILL_TRACE_TRIGGERS = (
        "3d view", "3-d view", "3d ", "3-d ",
        "drill trace", "drill traces", "trace 3d",
        "hole geometry", "show me hole geometry",
        "drillhole 3d", "drillhole geometry",
    )
    if (
        any(t in q_lower for t in _DRILL_TRACE_TRIGGERS)
        and _allowed("query_drill_traces_3d")
        and not any(name == "query_drill_traces_3d" for name, _ in results)
    ):
        # Hole-id short-circuit takes precedence: a query naming a hole
        # gets the single-hole variant, regardless of intent.
        effective_intent = state.effective_intent or state.intent
        if hole_ids or effective_intent in (
            "synthesis", "project_summary", "factual_lookup",
        ):
            result = await _call_tool_safely(
                "query_drill_traces_3d", state.query, state.deps,
            )
            if result is not None and getattr(result, "count", 1):  # AGT-4
                results.append(("query_drill_traces_3d", result))
                logger.info(
                    "agentic_retrieval.execute: drill_trace_3d card triggered "
                    "(keyword match, hole_id=%s)",
                    hole_ids[0] if hole_ids else None,
                )

    logger.info(
        "agentic_retrieval.execute: %d tool result(s) collected", len(results)
    )

    # Plan §3a/§3b wiring — build a typed EvidencePacket alongside the
    # legacy tool_results list. Best-effort: converter failures (malformed
    # tool payload, unknown row shape) log + stash a None packet rather
    # than break the answer path. Downstream consumers must tolerate
    # `state.evidence_packet is None` exactly the way they tolerate an
    # empty tool_results list today.
    evidence_packet = None
    try:
        # Use the answer_run_id when available so the packet's query_id
        # matches the row written by persist_node. Otherwise fall back to
        # a fresh UUID — the packet will still be coherent within its own
        # lifetime; only cross-table joins lose it.
        from uuid import uuid4 as _uuid4  # noqa: PLC0415

        from app.agent.authority import (  # noqa: PLC0415
            annotate_evidence_packet_with_authority,
            rank_evidence_by_authority,
        )
        from app.agent.evidence_converter import build_evidence_packet  # noqa: PLC0415
        from app.config import settings as _cfg  # noqa: PLC0415
        _query_id = str(_uuid4())

        # The real context ceiling for the active backend, the same one
        # context_prep trims against. Without it the packet fell back to
        # build_evidence_packet's 6500 default -- a number from the retired
        # 16K-context Qwen host -- and the chat UI's Budget pill showed
        # twelve ordinary chunks as -7,000 tokens over budget on a 128K model.
        raw_packet = build_evidence_packet(
            query_id=_query_id,
            query_text=state.query,
            tool_results=results,
            system_prompt_tokens=state.system_prompt_tokens_estimate or 0,
            max_context_tokens=_cfg.effective_max_context_tokens,
        )
        # Refresh authority_rank from document_type, then re-sort the
        # packet so high-authority evidence reads first. assemble_node
        # leaves context_block construction on the legacy tool_results
        # path for now (changing the LLM context shape is a separate,
        # higher-risk wire) — but downstream telemetry already benefits
        # from the canonical order.
        annotated = annotate_evidence_packet_with_authority(raw_packet)
        evidence_packet = rank_evidence_by_authority(annotated)
        logger.info(
            "agentic_retrieval.execute: built EvidencePacket "
            "(kinds=%s, total_tokens=%d, remaining_budget=%d)",
            sorted({e.kind for e in evidence_packet.evidence}),
            evidence_packet.total_tokens,
            evidence_packet.remaining_budget,
        )
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "agentic_retrieval.execute: EvidencePacket build failed — "
            "downstream consumers will see evidence_packet=None"
        )

    return {
        "tool_results": results,
        "evidence_packet": evidence_packet,
        "retrieval_failures": retrieval_failures,
    }


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------


def _worth_citing(tool_name: str, result: Any) -> bool:
    """Keep a tool result only if it carries rows worth citing.

    `_is_empty_tool_result` has existed since Phase F.4, is documented as
    mandatory — "empty tool results must be dropped *before* citation
    assignment so they don't produce zero-relevance citations that trip the
    Layer 1 retrieval_quality gate" — is exported in `__all__`, and had zero
    call sites anywhere in the tree.

    The cost was quiet rather than loud. `_compute_confidence` takes the
    arithmetic MEAN of per-result relevance, so a strong document answer
    paired with one structured lookup that returned zero rows reported
    roughly half the confidence it had earned. The empty result also took a
    line in the LLM's Evidence Set block and drew a citation marker for a
    tool that found nothing.

    Visualization cards (query_stereonet, query_drill_traces_3d) are NOT
    routed through this. They are not evidence, and `_is_empty_tool_result`
    would not recognise their types anyway — it returns False for anything
    it does not know, so passing them through would be a silent no-op that
    reads as coverage.
    """
    if result is None:
        return False

    from app.agent.tool_result_helpers import _is_empty_tool_result  # noqa: PLC0415

    if _is_empty_tool_result(result):
        logger.info(
            "agentic_retrieval.execute: dropped empty %s result before "
            "citation assignment",
            tool_name,
        )
        return False
    return True


def _categories_from_tool_results(
    tool_results: list[tuple[str, Any]],
) -> dict[str, bool]:
    """Which shapes of evidence actually made it into the context block.

    Feeds ``orchestrator._select_system_prompt``, which picks between the
    DEFAULT / NUMERIC / NARRATIVE / GRAPH system-prompt variants.

    Until 2026-08-21 both production call sites passed ``categories=None``,
    and ``_select_system_prompt`` short-circuits to DEFAULT on a falsy
    ``categories`` before any branch runs. NUMERIC, NARRATIVE, GRAPH and the
    ``SYSTEM_PROMPT_ROUTING_ENABLED`` flag were therefore dead on the live
    path: the only callers that ever reached the routing logic were two test
    modules. A count query ("how many holes exceeded 500 m?") never received
    the NUMERIC variant whose entire purpose is "quote verbatim from
    HIGH-CONFIDENCE SUMMARIES", and a document-heavy NI 43-101 question never
    received NARRATIVE's paraphrase-fidelity discipline.

    The in-code justification was that "the per-intent base preamble doesn't
    change here, only the answer-emphasis suffix does" — but that suffix is
    appended only under ``GEO_ANSWER_OIUR_ENABLED``, which is False in
    production. With both mechanisms off, every query in production got the
    same unshaped DEFAULT prompt.

    Derived from RESULTS rather than from the classifier's intent guess.
    ``assemble_node`` runs after ``execute_node``, so by this point we know
    what evidence exists rather than what the query looked like — and the
    variant's job is to tell the model how to handle the evidence it is
    about to read. A classifier that predicted "assay" for a query whose
    assay lookup returned nothing would otherwise select NUMERIC for a
    context containing only prose.

    Visualization results (``query_stereonet``, ``query_drill_traces_3d``)
    are deliberately excluded: they are rendered as cards, are not evidence,
    and do not appear in the context block that the variant is shaping.
    """
    from app.agent.public_geoscience_tool import (  # noqa: PLC0415
        PublicGeoscienceSearchResult,
    )
    from app.agent.tools import (  # noqa: PLC0415
        AssayDataResult,
        CollarDetailsResult,
        CoverageGapResult,
        DocumentSearchResult,
        DownholeLogsResult,
        GraphTraversalResult,
        ProjectOverviewResult,
        ProjectSummaryResult,
        SpatialQueryResult,
    )

    buckets: dict[str, tuple[type, ...]] = {
        "documents": (DocumentSearchResult,),
        "public_geo": (PublicGeoscienceSearchResult,),
        "graph": (GraphTraversalResult,),
        "spatial": (SpatialQueryResult, CollarDetailsResult),
        "assay": (AssayDataResult,),
        "downhole": (DownholeLogsResult,),
        "overview": (
            ProjectOverviewResult,
            ProjectSummaryResult,
            CoverageGapResult,
        ),
    }

    categories: dict[str, bool] = {}
    for _tool_name, result in tool_results:
        if result is None:
            continue
        for name, types in buckets.items():
            if isinstance(result, types):
                categories[name] = True
                break
    return categories


# Per structured (non-chunk) block. Was 1,200 characters of
# ``str(result)``, which for a dataclass whose row list is declared first
# (AssayDataResult.samples, SpatialQueryResult.collars,
# DownholeLogsResult.intervals) meant the model saw about five rows and
# NONE of count / min / max / mean / median (audit AGT-2 / RAG-5).
_STRUCTURED_CAP = 4000
_STRUCTURED_ROW_SAMPLE = 12
_SCALAR_REPR_CAP = 240


def _short(value: Any, cap: int = _SCALAR_REPR_CAP) -> str:
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= cap:
        return text
    return f"{text[:cap]}...(+{len(text) - cap} chars)"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _render_assay_result(result: Any) -> str:
    """Header-first rendering of an AssayDataResult (AGT-2, AGT-15)."""
    lines: list[str] = []
    holes = list(getattr(result, "hole_filter", None) or [])
    scope = f"holes {', '.join(holes)}" if holes else "whole project"
    lines.append(
        f"assay element={result.element} scope={scope} "
        f"sample_count={result.count} (all matching samples, not just the rows shown)"
    )
    lines.append(
        f"aggregates over all {result.count} samples of {result.element}: "
        f"min={_fmt_num(result.min_value)} max={_fmt_num(result.max_value)} "
        f"mean={_fmt_num(result.mean_value)} median={_fmt_num(result.median_value)}"
    )
    requested = getattr(result, "requested_commodity", None)
    if getattr(result, "requested_commodity_unavailable", False) and requested:
        lines.append(
            f"NOTE: the question asks about {requested}, but no assay values "
            f"for {requested} exist in this scope. The statistics here are "
            f"for other elements; do not present them as {requested} grades."
        )
    summaries = list(getattr(result, "element_summaries", None) or [])
    if getattr(result, "element_auto_selected", False) and summaries:
        lines.append(
            f"NOTE: no single commodity was named, so {result.element} was "
            f"picked only to populate the rows below; the aggregates above "
            f"are for {result.element} alone. Per-element summaries:"
        )
        for s in summaries:
            lines.append(
                f"  {s.element}: n={s.count} min={_fmt_num(s.min_value)} "
                f"max={_fmt_num(s.max_value)} mean={_fmt_num(s.mean_value)} "
                f"median={_fmt_num(s.median_value)}"
            )
    available = list(getattr(result, "available_elements", None) or [])
    if available:
        lines.append(f"assayed elements in project: {', '.join(available[:40])}")
    samples = list(getattr(result, "samples", None) or [])
    top = sorted(
        samples, key=lambda s: (s.value is not None, s.value or 0.0), reverse=True,
    )[:_STRUCTURED_ROW_SAMPLE]
    if top:
        lines.append(
            f"highest {len(top)} of {result.count} samples by {result.element} "
            f"(hole, from_depth-to_depth, value, sample_type):"
        )
        for s in top:
            lines.append(
                f"  {s.hole_id} {_fmt_num(s.from_depth)}-{_fmt_num(s.to_depth)} "
                f"{_fmt_num(s.value)} {s.sample_type}"
            )
    return "\n".join(lines)


def _render_structured_result(result: Any) -> str:
    """Render one structured tool result: scalars first, then row samples.

    Generic over the tool dataclasses so every one of them gets the fix,
    not just the three the audit reproduced: scalar fields (count,
    aggregates, data_source) come first, nested records next, then each
    list as "showing k of N" with a bounded sample.
    """
    from app.agent.tools import AssayDataResult  # noqa: PLC0415

    if isinstance(result, AssayDataResult):
        return _render_assay_result(result)[:_STRUCTURED_CAP]
    if not dataclasses.is_dataclass(result) or isinstance(result, type):
        return _short(result, _STRUCTURED_CAP)

    scalars: list[str] = []
    nested: list[str] = []
    lists: list[tuple[str, list[Any]]] = []
    for f in dataclasses.fields(result):
        value = getattr(result, f.name, None)
        if isinstance(value, (list, tuple)):
            lists.append((f.name, list(value)))
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            nested.append(f"{f.name}: {_short(value, 800)}")
        else:
            scalars.append(f"{f.name}={_short(value)}")
    lines = [" ".join(scalars)] if scalars else []
    lines.extend(nested)
    for name, items in lists:
        shown = items[:_STRUCTURED_ROW_SAMPLE]
        lines.append(f"{name}: showing {len(shown)} of {len(items)}")
        lines.extend(f"  {_short(item, 400)}" for item in shown)
    return "\n".join(lines)[:_STRUCTURED_CAP]


@dataclasses.dataclass(frozen=True)
class _ContextBlock:
    tool_index: int
    sub_index: int
    kind: str  # "structured" | "chunk" | "record"
    score: float
    text: str
    tool_name: str
    citation_id: str


def _build_context_blocks(
    tool_results: list[tuple[str, Any]], *, fence: bool,
) -> list[_ContextBlock]:
    """One block per chunk / public-geo record / structured result."""
    from app.agent.context_builder import _fence_untrusted  # noqa: PLC0415
    from app.agent.response_assembler import assign_citation_ids  # noqa: PLC0415

    # Imported, not re-typed. Chunks are already bounded at ingest to
    # WINDOW_CHARS, and this used to cut them again at 1,800 — 64% of every
    # retrieved chunk was discarded before the model saw it, while the
    # citation for that chunk went into the answer regardless. A citation
    # pointing at text nobody read is worse than no citation: it looks
    # like evidence. pdf_report's own sizing comment says the window was
    # chosen so five chunks land at ~6,250 tokens; this cap was what stopped
    # that from happening.
    from app.services.ingest.pdf_report import WINDOW_CHARS  # noqa: PLC0415

    blocks: list[_ContextBlock] = []
    id_bundles = assign_citation_ids(tool_results)
    for ti, ((tool_name, result), bundle) in enumerate(
        zip(tool_results, id_bundles, strict=False)
    ):
        chunks = getattr(result, "chunks", None)
        records = getattr(result, "records", None)
        if chunks is not None:
            # DocumentSearchResult-shaped — one block per retrieved chunk,
            # each carrying its OWN citation id (audit 2026-08-14 finding 1:
            # assign_citation_ids emits one id per chunk, mirroring the PGEO
            # per-record branch, so the marker the model cites maps to the
            # real chunk_id/section/page in the assembled Citation). zip is
            # deliberately non-strict: an empty result has one sentinel id
            # and zero chunks.
            fallback_cid = bundle[0] if bundle else "[DATA-0]"
            for idx, chunk in enumerate(chunks):
                cid = bundle[idx] if idx < len(bundle) else fallback_cid
                header = (
                    f"{cid} "
                    f"{getattr(chunk, 'document_title', None) or 'Untitled document'}"
                )
                section = (
                    getattr(chunk, "section_number", None)
                    or getattr(chunk, "section_title", None)
                    or getattr(chunk, "section", None)
                )
                if section:
                    header += f" | section {section}"
                page = getattr(chunk, "page", None) or getattr(chunk, "page_number", None)
                if page is not None:
                    header += f" | page {page}"
                # annotated_text, not text: a page-image chunk's content is a
                # vision model's DESCRIPTION of the page, and annotated_text
                # carries the "not quoted text from the document" prefix
                # (and the OCR-quality warning).
                text = (getattr(chunk, "annotated_text", None)
                        or getattr(chunk, "text", "")
                        or "")[:WINDOW_CHARS]
                if fence:
                    text = _fence_untrusted(text)
                score = getattr(chunk, "relevance_score", None)
                blocks.append(_ContextBlock(
                    ti, idx, "chunk",
                    float(score) if isinstance(score, (int, float)) else 0.0,
                    f"{header}\n{text}", tool_name, cid,
                ))
        elif records is not None and len(bundle) == len(records):
            # PublicGeoscienceSearchResult-shaped — one id per record.
            for idx, (record, cid) in enumerate(zip(records, bundle, strict=False)):
                record_text = _short(record, 1200)
                if fence:
                    record_text = _fence_untrusted(record_text)
                blocks.append(_ContextBlock(
                    ti, idx, "record", 0.0,
                    f"{cid} tool={tool_name} {record_text}", tool_name, cid,
                ))
        else:
            # Structured results (collars / samples / overview / ...) — one
            # header-first block per tool result. Sourced from our own
            # PostGIS tables, not externally-authored free text, so
            # (matching _build_context) these are NOT fenced.
            cid = bundle[0] if bundle else "[DATA-0]"
            blocks.append(_ContextBlock(
                ti, 0, "structured", 0.0,
                f"{cid} tool={tool_name}\n{_render_structured_result(result)}",
                tool_name, cid,
            ))
    return blocks


def _select_blocks_for_budget(
    blocks: list[_ContextBlock], budget: int,
) -> set[tuple[int, int]]:
    """Which blocks fit, by priority rather than dispatch order (RAG-18).

    Dispatch order used to decide: search_documents runs first for
    synthesis, twelve ~5,000-char chunks exhausted the budget, and the
    collar/assay blocks became "[context budget reached]" while still
    counting as evidence. Now:

      1. structured blocks (compact since AGT-2), up to half the budget —
         the first one always;
      2. document chunks, highest relevance first;
      3. structured blocks that overflowed step 1;
      4. public-geoscience records.

    A block that does not fit is skipped, not a stopping point, so a
    shorter lower-ranked block can still use the remaining room.
    """
    sep = 2  # blank line between blocks
    chosen: set[tuple[int, int]] = set()
    total = 0
    structured_total = 0

    def _try(block: _ContextBlock) -> bool:
        nonlocal total
        size = len(block.text) + sep
        if total + size > budget:
            return False
        chosen.add((block.tool_index, block.sub_index))
        total += size
        return True

    overflow: list[_ContextBlock] = []
    for block in (b for b in blocks if b.kind == "structured"):
        size = len(block.text) + sep
        if structured_total and structured_total + size > budget // 2:
            overflow.append(block)
            continue
        if _try(block):
            structured_total += size
        else:
            overflow.append(block)
    chunks = sorted(
        (b for b in blocks if b.kind == "chunk"), key=lambda b: b.score, reverse=True,
    )
    for block in chunks:
        _try(block)
    for block in overflow:
        _try(block)
    for block in (b for b in blocks if b.kind == "record"):
        _try(block)
    return chosen


def _prune_tool_results(
    tool_results: list[tuple[str, Any]],
    blocks: list[_ContextBlock],
    kept: set[tuple[int, int]],
) -> list[tuple[str, Any]]:
    """tool_results restricted to the blocks that were rendered."""
    kinds = {b.tool_index: b.kind for b in blocks}
    pruned: list[tuple[str, Any]] = []
    for ti, (tool_name, result) in enumerate(tool_results):
        kind = kinds.get(ti)
        if kind is None:
            # Rendered nothing at all (an empty document search keeps its
            # sentinel citation for the refusal path) — leave it as it was.
            pruned.append((tool_name, result))
        elif kind == "structured":
            if (ti, 0) in kept:
                pruned.append((tool_name, result))
        else:
            attr = "chunks" if kind == "chunk" else "records"
            items = [
                item for idx, item in enumerate(getattr(result, attr))
                if (ti, idx) in kept
            ]
            if not items:
                continue
            changes = {attr: items, "count": len(items)}
            try:
                pruned.append((tool_name, dataclasses.replace(result, **changes)))
            except (TypeError, ValueError):
                # Not a dataclass we can rebuild; keep it whole rather than
                # lose evidence that was (partly) rendered.
                logger.debug(
                    "agentic_retrieval.assemble: could not prune %s result "
                    "(%s); keeping it whole", tool_name, type(result).__name__,
                    exc_info=True,
                )
                pruned.append((tool_name, result))
    return pruned


def _render_tool_results_context(
    tool_results: list[tuple[str, Any]],
    *,
    query: str | None = None,
    workspace_id: Any = None,
) -> str:
    """Render tool results into the LLM context block, chunk by chunk.

    Replaces the legacy ``f"[DATA:{n}] tool=... result={result!r}"[:1500]``
    rendering, which handed the model a truncated Python repr — the actual
    retrieved passage text rarely survived the 1500-char cut, and the
    ``[DATA:n]`` labels never matched the ``[NI43-n]``/``[PUB-n]``/
    ``[PGEO-n]``/``[DATA-n]`` ids the assembler attaches to the response
    citations (the model cited [DATA:3] while the UI chips said [NI43-3]).

    Uses ``assign_citation_ids`` so every block carries the SAME canonical
    id ``assemble_response`` will emit for that tool result — that
    alignment is the point.

    Prompt-injection fencing (RAG-safety audit 2026-08-15): this is the
    ONLY renderer on the live agentic-retrieval path (assemble_node calls
    it whenever ``CONTEXT_PREP_ENABLED`` is off, which is the default) —
    ``app.agent.context_builder._build_context`` implements the same
    fencing but is dead code (confirmed zero call sites outside the
    retired legacy orchestrator's re-export and tests). Reusing its
    ``_fence_untrusted``/``_UNTRUSTED_GUARD`` here, gated by the SAME
    ``settings.PROMPT_INJECTION_DELIMITING_ENABLED`` flag (default True
    since 2026-08-21; it was False for the first eight weeks, and live
    ``fastapi-cc`` never set it, so this path ran unfenced) closes that
    gap: the fence now protects the path real traffic runs through, not
    just the abandoned one. Fencing is applied to the two
    externally-sourced free-text surfaces the model sees — retrieved
    document-chunk text and public-geoscience record dumps — mirroring
    exactly what ``_build_context`` fences (structured PostGIS/Neo4j/
    collar/graph blocks stay unfenced, same as there).


    Budget (RAG-18, 2026-09-29): ``_TOTAL_BUDGET`` caps this block. Which
    blocks make the cut is decided by ``_select_blocks_for_budget``
    (structured data first, then chunks by relevance), and what was cut
    is no longer merely logged: :func:`_render_context_and_evidence`
    returns the tool results restricted to what was rendered, and
    assemble_node hands THAT list to citation assembly and the guards, so
    a block the model never read can neither be cited nor ground a number.
    """
    text, _ = _render_context_and_evidence(
        tool_results, query=query, workspace_id=workspace_id,
    )
    return text


def _render_context_and_evidence(
    tool_results: list[tuple[str, Any]],
    *,
    query: str | None = None,
    workspace_id: Any = None,
) -> tuple[str, list[tuple[str, Any]]]:
    """Render the context block; return it with the evidence it contains.

    The second element is ``tool_results`` itself (same object) when
    everything fit, otherwise a pruned copy in which document chunks and
    public-geo records that were dropped are removed from their results,
    and dropped structured results are removed entirely. Citation ids are
    assigned over the pruned list, so the markers in the prompt are the
    ones ``assemble_response`` will emit for it.
    """
    from app.agent.context_builder import _UNTRUSTED_GUARD  # noqa: PLC0415
    from app.config import settings as _settings  # noqa: PLC0415

    # Derived from the active backend's context window rather than frozen.
    # 24,000 characters was sized for the 16K-context local vLLM deployment
    # and never revisited after the move to a 100,000-token hosted window,
    # so the evidence budget was about 6% of what was available.
    #
    # ~4 chars/token is the usual English approximation. The clamp is the
    # point of the expression: spend a documented slice of the window, and
    # never let a backend with a huge window turn every query into a
    # 100K-token bill. At 48,000 characters the top five 5,000-char chunks
    # fit whole with room for the structured blocks beside them.
    _CHARS_PER_TOKEN = 4
    _TOTAL_BUDGET = min(
        48_000,
        int(_settings.effective_max_context_tokens * 0.30) * _CHARS_PER_TOKEN,
    )
    fence = bool(_settings.PROMPT_INJECTION_DELIMITING_ENABLED)
    guard_cost = len(_UNTRUSTED_GUARD) + 2 if fence else 0

    blocks = _build_context_blocks(tool_results, fence=fence)
    kept = _select_blocks_for_budget(blocks, max(0, _TOTAL_BUDGET - guard_cost))
    rendered_results = tool_results
    omitted = [b for b in blocks if (b.tool_index, b.sub_index) not in kept]
    if omitted:
        rendered_results = _prune_tool_results(tool_results, blocks, kept)
        blocks = _build_context_blocks(rendered_results, fence=fence)
        logger.warning(
            "agentic_retrieval.assemble: context budget reached (%d chars) "
            "— dropped %d block(s) totalling ~%d chars from the LLM context "
            "AND from the evidence citations/guards see (dropped: %s); query=%r "
            "workspace_id=%s",
            _TOTAL_BUDGET,
            len(omitted),
            sum(len(b.text) for b in omitted),
            [f"{b.tool_name}:{b.citation_id}" for b in omitted[:25]],
            query,
            workspace_id,
        )

    out: list[str] = []
    if fence and blocks:
        out.extend([_UNTRUSTED_GUARD, ""])
    out.extend(b.text for b in blocks)
    if omitted:
        out.append(
            f"[context budget reached: {len(omitted)} lower-ranked evidence "
            f"block(s) omitted; they are not available to cite]"
        )
    return ("\n\n".join(out) if out else "(no tool results)"), rendered_results


def _question_for_llm(state: AgenticRetrievalState) -> str:
    """The USER QUESTION the synthesis model is shown.

    Audit AGT-1: when resolve_node rewrote the query, the rewrite used to
    REPLACE the user's words in the prompt, so a bad substitution was the
    only version of the question the model ever saw. The model now gets
    the user's own words first, with the rewrite as a labelled reading it
    can use to resolve "it"/"its" against the conversation.
    """
    original = (state.query_original or "").strip()
    rewritten = (state.query or "").strip()
    if not original or original == rewritten:
        return state.query
    return (
        f"{original}\n"
        f"(Read in the context of the earlier conversation as: {rewritten})"
    )


async def assemble_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Build the LLM context, call the model, and assemble the response.

    Heavy lifting delegates to:
      * ``app.agent.llm_calls._call_llm`` — context build + LLM dispatch
      * ``app.agent.response_assembler.assemble_response`` — citation
        assembly + OIUR parse + Stage-1 confidence (Phase 1.3)

    The prompt selection currently uses the same ``_select_system_prompt``
    helper as the legacy orchestrator, plus the OIUR + decision-support
    rule blocks when their flags are set. Phase 2 does NOT introduce
    intent-specific prompt variants in 2.3 — that lands as an enhancement
    in 2.4 / 2.5 once we have real-corpus telemetry.
    """
    from app.agent.hallucination.layer1_retrieval import (  # noqa: PLC0415
        assess_retrieval_quality,
        build_refusal_payload,
        build_refusal_text,
    )
    from app.agent.llm_calls import _call_llm  # noqa: PLC0415
    from app.agent.orchestrator import _select_system_prompt  # noqa: PLC0415
    from app.agent.response_assembler import assemble_response  # noqa: PLC0415
    from app.config import settings as _settings  # noqa: PLC0415

    # Layer 1 — retrieval quality gate (hard half), restored 2026-09-24 per
    # CLAUDE.md hard rule 5 / §04i. Runs BEFORE the LLM is ever called: if
    # nothing cleared the relevance floor from ANY store (document search's
    # own per-chunk floor has already run inside search_documents by this
    # point), there is no point spending an LLM call synthesizing an answer
    # from empty context, and no reliable way for a post-hoc guard to tell
    # a careful refusal apart from a confident fabrication built on
    # nothing. See app.agent.hallucination.layer1_retrieval for the full
    # verdict logic, including why cosine/RRF-fallback scores never drive
    # this decision.
    _l1_verdict = assess_retrieval_quality(
        state.tool_results,
        intent=state.effective_intent or state.intent,
        query=state.query,
    )
    if _l1_verdict.refuse:
        logger.warning(
            "agentic_retrieval.assemble: Layer 1 retrieval quality gate "
            "refused this query -- %s",
            _l1_verdict.reason,
        )
        try:
            from app.metrics import RETRIEVAL_GATE_REFUSED_TOTAL  # noqa: PLC0415

            RETRIEVAL_GATE_REFUSED_TOTAL.inc()
        except Exception:  # noqa: BLE001 — metrics must never break the gate
            logger.debug("RETRIEVAL_GATE_REFUSED_TOTAL increment failed", exc_info=True)
        if state.status_callback is not None:
            try:
                await state.status_callback("No relevant evidence found…")
            except Exception:  # pragma: no cover — status is a UX affordance
                logger.debug(
                    "agentic_retrieval.assemble: status_callback raised",
                    exc_info=True,
                )
        if state.retrieval_failures:
            # Audit RAG-12: the refusal text says nothing cleared the
            # relevance floor. With a document search that never completed
            # that is false — the corpus was not searched — so fail the
            # query instead (no LLM call either way; the Layer 1 hard gate
            # still holds).
            from app.agent.errors import RetrievalBackendUnavailable  # noqa: PLC0415

            raise RetrievalBackendUnavailable(
                "; ".join(state.retrieval_failures)
            )
        response = assemble_response(build_refusal_text(), state.tool_results)
        # CHAT-10 — stamp the machine-readable refusal UNCONDITIONALLY.
        # refusal_payload used to be set only by repair_stage2 behind
        # REPAIR_LOOP_TERMINAL_ENABLED (off everywhere), so this — the
        # primary refusal path — reached the chat as ordinary answer text
        # with a "conf 0.10" pill, and RefusalPanel never rendered. This
        # does not touch the flag's own behaviour (terminal repair
        # strategies still stamp only when it is on).
        response.refusal_payload = build_refusal_payload()
        return {"response": response, **_fold_token_usage(state)}

    # Plan §3 — when CONTEXT_PREP_ENABLED is set, run the EvidencePacket
    # through the per-intent prepare_evidence_for_intent pipeline. The
    # prepared packet replaces state.evidence_packet AND drives the
    # context_block. When the flag is off (default) the legacy
    # tool_results → context_block path runs as before.
    #
    # Best-effort: any failure inside the prep pipeline logs but does
    # NOT block the answer path — we fall back to the legacy path so
    # the user still gets an answer (degraded, but not broken).
    if (
        _settings.CONTEXT_PREP_ENABLED
        and state.evidence_packet is not None
        and state.evidence_packet.evidence
    ):
        try:
            from app.agent.context_prep import (  # noqa: PLC0415
                prepare_evidence_for_intent,
            )
            effective_intent = state.effective_intent or state.intent
            prepared = prepare_evidence_for_intent(
                state.evidence_packet,
                effective_intent,
                # effective_max_context_tokens is a @property — returns
                # the right ceiling for the active LLM backend
                # (Anthropic 200K vs vLLM 22K).
                max_context_tokens=getattr(
                    _settings, "effective_max_context_tokens",
                    _settings.MAX_CONTEXT_TOKENS,
                ),
            )
            state.evidence_packet = prepared.packet
            # Plan §3 — stash the audit payload on state so persist_node
            # writes it to silver.query_traces.context_prep_audit JSONB.
            try:
                state.context_prep_audit_payload = {
                    "intent": prepared.intent,
                    "quota_used": dict(prepared.quota_used),
                    "reached_budget": prepared.reached_budget,
                    "dropped_evidence_ids": list(prepared.dropped_evidence_ids),
                    "budget_reason": prepared.budget_reason,
                    "kind_distribution_before": dict(prepared.kind_distribution_before),
                    "kind_distribution_after": dict(prepared.kind_distribution_after),
                }
            except Exception:  # pragma: no cover — defensive
                logger.debug(
                    "agentic_retrieval.assemble: failed to stash "
                    "context_prep_audit_payload",
                    exc_info=True,
                )
            logger.info(
                "agentic_retrieval.assemble: context_prep applied "
                "(intent=%s, kinds_before=%s, kinds_after=%s, "
                "reached_budget=%s, dropped=%d)",
                prepared.intent,
                prepared.kind_distribution_before,
                prepared.kind_distribution_after,
                prepared.reached_budget,
                len(prepared.dropped_evidence_ids),
            )
        except Exception:  # pragma: no cover — defensive
            logger.exception(
                "agentic_retrieval.assemble: context_prep failed — "
                "falling back to legacy tool_results context"
            )

    # Build the LLM context block. Two paths:
    #   1. CONTEXT_PREP_ENABLED + non-empty prepared packet → render
    #      from the typed evidence (authority-ranked, diversity-balanced,
    #      budget-fit).
    #   2. Default → typed per-chunk tool_results rendering with the
    #      canonical citation ids (see _render_tool_results_context).
    #
    # RAG-safety audit 2026-08-15 — KNOWN OPEN GAP, do not flip
    # CONTEXT_PREP_ENABLED without fixing this first: the two paths use
    # DISJOINT citation-marker vocabularies. Path 2 (below, and the
    # default) tags each block with the SAME dash-form id
    # (``[NI43-n]``/``[PGEO-n]``/``[DATA-n]``) that
    # ``response_assembler.assign_citation_ids`` will later emit as the
    # real ``Citation.citation_id`` — see ``_render_tool_results_context``'s
    # docstring for why that lockstep matters. Path 1 instead assigns a
    # fresh sequential colon-form id (``[DATA:1]``, ``[DATA:2]``, ...)
    # over ``state.evidence_packet.evidence`` — a list that context_prep
    # has already re-ranked (authority order) and pruned (diversity quota
    # + token budget) relative to ``state.tool_results``. Meanwhile
    # ``assemble_response`` below is ALWAYS called with the raw
    # ``state.tool_results``, never with the prepared packet, so it always
    # emits dash-form ids in tool_results order. Net effect: when this
    # branch is live, the ids the model is told to cite
    # (``[DATA:1]``...) essentially never match any real
    # ``Citation.citation_id`` (``[NI43-3]``...), so
    # ``hallucination.layer2_typed_output`` strips the model's markers as
    # orphans and citations silently collapse. Not fixed here — reconciling
    # it means either (a) making assemble_response consume the prepared
    # packet's own ids instead of re-deriving from tool_results, or
    # (b) rendering path 1 with the same assign_citation_ids ids restricted
    # to the surviving evidence — both are a real design decision for
    # whoever runs the "shadow → eval → GA" rollout this flag was staged
    # for (see config.py), not a one-line patch.
    context_lines: list[str] = []
    citation_counter = 0
    use_packet_for_context = (
        _settings.CONTEXT_PREP_ENABLED
        and state.evidence_packet is not None
        and state.evidence_packet.evidence
    )
    # The evidence the model is actually shown. Equal to state.tool_results
    # unless the context budget dropped blocks (RAG-18), in which case it
    # is the pruned list — and it, not the full retrieval, is what the
    # citations are built from and what validate_node's guards check
    # against (returned as "tool_results" below).
    rendered_results: list[tuple[str, Any]] = state.tool_results
    if use_packet_for_context:
        for ev in state.evidence_packet.evidence:
            citation_counter += 1
            # The repr() form keeps a single-line, typed surface for the
            # LLM — same shape as the legacy path but the rows are now
            # in authority order and the kind list reflects §3c diversity.
            ev_summary = (
                f"[DATA:{citation_counter}] kind={ev.kind} "
                f"evidence_id={ev.evidence_id} {ev.model_dump(mode='json')!r}"
            )
            context_lines.append(ev_summary[:1500])
        context_block = (
            "\n".join(context_lines) if context_lines else "(no tool results)"
        )
    else:
        context_block, rendered_results = _render_context_and_evidence(
            state.tool_results,
            query=state.query,
            workspace_id=getattr(state.deps, "workspace_id", None),
        )

    # System-prompt variant selection. Derived from the evidence actually
    # retrieved — see _categories_from_tool_results for why this used to be
    # a hardcoded `categories=None` and what that cost. Set
    # SYSTEM_PROMPT_ROUTING_ENABLED=false to go back to always-DEFAULT
    # without a deploy.
    system_prompt = _select_system_prompt(
        categories=_categories_from_tool_results(rendered_results),
        query=state.query,
    )

    # Plan §0b — estimate static system-prompt tokens for the trace.
    # chars/4 is the cheap-and-good-enough proxy used by plan §0b's
    # CC-5 budget arithmetic. A precise tokenizer call would lock us
    # into Qwen3-14B-AWQ; that's fine in production but pollutes the
    # critical path with the tokenizer import. Stash on state so
    # persist_node can write it to silver.query_traces.system_prompt_tokens.
    try:  # noqa: SIM105
        state.system_prompt_tokens_estimate = max(1, len(system_prompt) // 4)
    except Exception:  # pragma: no cover — defensive (state attr immutable etc.)
        pass

    # Plan §3a/§3f — refresh the EvidencePacket's remaining_budget now
    # that we have a real system_prompt_tokens estimate. execute_node
    # built the packet with system_prompt_tokens=0 (the prompt hadn't
    # been built yet); recompute so persist_node + downstream consumers
    # read a budget that reflects the live prompt size. Best-effort: a
    # missing packet (converter failure) is a no-op.
    if state.evidence_packet is not None:
        try:
            sp_tokens = state.system_prompt_tokens_estimate or 0
            new_remaining = (
                state.evidence_packet.remaining_budget
                + state.evidence_packet.system_prompt_tokens
                - sp_tokens
            )
            state.evidence_packet = state.evidence_packet.model_copy(update={
                "system_prompt_tokens": sp_tokens,
                "remaining_budget": new_remaining,
            })
        except Exception:  # pragma: no cover — defensive
            logger.debug(
                "agentic_retrieval.assemble: EvidencePacket budget refresh "
                "failed (non-fatal)",
                exc_info=True,
            )

    # Step 2.5 — append the per-intent answer-emphasis fragment matching
    # the retrieval profile. Empty when there is no profile, and empty when
    # GEO_ANSWER_OIUR_ENABLED is off — the fragments reference sections that
    # only exist inside the OIUR block. That second condition is enforced
    # inside fragment_for(); it used to be asserted only by this comment.
    if state.retrieval_profile is not None:
        try:
            from app.agent.prompts.answer_emphasis_section import (  # noqa: PLC0415
                fragment_for,
            )
            emphasis_fragment = fragment_for(state.retrieval_profile.answer_emphasis)
        except Exception:  # pragma: no cover — defensive
            logger.exception("agentic_retrieval.assemble: emphasis import failed")
            emphasis_fragment = ""
        if emphasis_fragment:
            system_prompt = system_prompt + emphasis_fragment

    # Step 3.1 / 3.3 — append the pre-processor's prompt suffixes
    # (reporting code reference + Field-mode 300-word cap when active).
    if state.retrieval_filters is not None:
        for suffix in state.retrieval_filters.prompt_suffixes:
            system_prompt = system_prompt + suffix

    # Phase 4 / gate criterion — when the active profile is the anomaly
    # subgraph (surface_qa_qc_fields=True), inspect the tool results for
    # the new QA/QC fields and append a prompt-hint describing availability.
    # When the new fields are absent the hint instructs graceful degrade to
    # the legacy Silver Review qaqc_flag column.
    if (
        state.retrieval_profile is not None
        and state.retrieval_profile.surface_qa_qc_fields
    ):
        try:
            from app.agent.agentic_retrieval.qaqc_availability import (  # noqa: PLC0415
                detect_qaqc_availability,
            )
            availability = detect_qaqc_availability(state.tool_results)
            qaqc_hint = availability.to_prompt_hint()
            if qaqc_hint:
                system_prompt = system_prompt + qaqc_hint
                logger.info(
                    "agentic_retrieval.assemble: appended QA/QC availability hint "
                    "(rows=%d, has_new_qaqc=%s, has_legacy_flag=%s)",
                    availability.inspected_rows,
                    availability.has_any_new_qaqc,
                    availability.has_legacy_qaqc_flag,
                )
        except Exception:  # pragma: no cover — defensive
            logger.exception(
                "agentic_retrieval.assemble: QA/QC availability detection failed"
            )

    openai_client = getattr(state.deps, "openai_http_client", None)
    anthropic_client = getattr(state.deps, "anthropic_client", None)

    # Step 2.5 — real token streaming. Previously this call passed no
    # token_callback at all, so _call_llm always took the blocking path
    # and queries.py's word-split synthetic-delta fallback ran on EVERY
    # query regardless of backend. _call_llm already knows how to stream
    # both Anthropic and OpenAI-compatible (incl. Azure Foundry) backends
    # once token_callback is set — the gap was purely that nothing in the
    # LangGraph path forwarded it.
    if state.status_callback is not None:
        try:
            await state.status_callback("Synthesizing answer…")
        except Exception:  # pragma: no cover — status is a UX affordance
            logger.debug("agentic_retrieval.assemble: status_callback raised", exc_info=True)

    # No local try/except around this call (deliberately — see below).
    # assemble_node used to catch every exception here and replace the
    # answer with a hardcoded "I was unable to generate a summary..."
    # string, then continue the pipeline as a success: HTTP 200, a
    # `completed` SSE event, real Citation objects built from whatever was
    # retrieved before the LLM call failed and attached to that boilerplate
    # text. A 429 from Foundry, a timeout, a content-filter trip, or
    # WorkspaceQuotaExceeded (the §35.1 cost-ceiling check _call_llm now
    # runs — see llm_calls.py) all got the identical treatment: a real
    # error dressed as a low-confidence answer. queries.py already has the
    # correct handling for this (a `failed` SSE event via classify_error(),
    # and a dedicated 429 path for WorkspaceQuotaExceeded) — it was simply
    # never reached because this local catch swallowed everything first.
    # Letting the exception propagate is what actually reaches it. Found in
    # a full-app review, 2026-08-05.
    text = await _call_llm(
        query=_question_for_llm(state),
        context=context_block,
        temperature=0.1,
        anthropic_client=anthropic_client,
        openai_http_client=openai_client,
        system_prompt=system_prompt,
        audit_label="agentic_retrieval",
        # The reader sees these tokens as the model produces them, and every
        # §04i guard runs afterwards: graph order is assemble -> validate ->
        # demote -> repair_shadow -> persist, and validate_node only ever
        # mutates the object that rides the `completed` frame. So for the
        # 15-30 s a synthesis takes, the text on screen has had zero
        # validation applied — orphan citation markers still present,
        # ungrounded numbers unflagged, no banner — and at `completed` it is
        # silently replaced.
        #
        # Holding the stream one guard behind was considered and rejected:
        # Layer 4 resolves entities against silver.collars and Layer 3
        # cross-checks numbers against tool results, so the checks are async
        # DB round-trips, not something that can run per-sentence without
        # turning streaming into a stutter. What ships instead is honesty
        # about the window — the message carries validation_state (default
        # "unverified"), and Chat.tsx renders a "not yet fact-checked" pill
        # while isStreaming and keeps it as "unverified" if the stream is cut
        # off before `completed` arrives.
        token_callback=state.token_callback,
        workspace_id=getattr(state.deps, "workspace_id", None),
        redis_client=getattr(state.deps, "redis_client", None),
        pg_pool=getattr(state.deps, "pg_pool", None),
    )

    # ADR-0007 PR-1 — for project_summary / coverage_gap, pre-build the
    # chat-card payloads from the structured tool results BEFORE the
    # response_assembler runs so the assembler attaches them to the
    # GeoRAGResponse it returns.
    map_payload, viz_payload = _build_chat_card_payloads(
        intent=state.effective_intent or state.intent,
        tool_results=state.tool_results,
    )
    response = assemble_response(
        text,
        rendered_results,
        map_payload=map_payload,
        viz_payload=viz_payload,
    )

    # Step 2.4 — surface unspecified envelope fields in the OIUR
    # uncertainty section so the geologist sees inline what the system
    # did not know. No-op when there's no geo_answer (OIUR flag off or
    # uncertainty is a SectionEmpty placeholder).
    response = _attach_envelope_notes_to_uncertainty(
        response,
        envelope_notes=state.envelope_notes,
        unspecified_descriptions=unspecified_field_descriptions(state.context_envelope),
    )
    response = _with_retrieval_failures(response, state.retrieval_failures)
    update: dict[str, Any] = {"response": response, **_fold_token_usage(state)}
    # Audit AGT-5: the in-place `state.X = ...` writes above are visible to
    # the rest of THIS node only. LangGraph rebuilds the state for the next
    # node from channels, so anything persist_node reads has to be in the
    # returned update — system_prompt_tokens was always NULL in
    # silver.query_traces, context_prep_audit likewise, and with
    # CONTEXT_PREP_ENABLED the pruned packet fed the prompt while persist
    # stamped the UNPRUNED one onto the response.
    update.update(_assemble_state_writes(state))
    if rendered_results is not state.tool_results:
        update["tool_results"] = rendered_results
    return update


def _with_retrieval_failures(
    response: GeoRAGResponse, failures: list[str],
) -> GeoRAGResponse:
    """Add document searches that did not complete to degraded_sources.

    response_assembler derives degraded_sources from tool_results, but a
    failed search returns an EMPTY result that _worth_citing drops before
    assembly, so the timeout never reached it (audit RAG-12).
    """
    if not failures:
        return response
    merged = list(dict.fromkeys([*(response.degraded_sources or []), *failures]))
    return response.model_copy(update={"degraded_sources": merged})


def _assemble_state_writes(state: AgenticRetrievalState) -> dict[str, Any]:
    """The state fields assemble_node sets in place, as an update dict."""
    return {
        "evidence_packet": state.evidence_packet,
        "system_prompt_tokens_estimate": state.system_prompt_tokens_estimate,
        "context_prep_audit_payload": state.context_prep_audit_payload,
    }


def _round_trace_points_for_card(points: list[dict]) -> list[dict]:
    """Round a drill trace for the chat card's wire payload only (CHAT-5).

    Full float64 reprs made the 3D card about 95 bytes per point, so a
    200-hole x 50-point project pushed the `completed` frame past Reverb's
    1 MB request limit and the chat never received its terminal frame.
    7 decimal places of a degree is about 1 cm and 2 of a metre is 1 cm,
    finer than any collar survey; the card is a visual. Tool results — what
    the §04i guards verify numbers against — are untouched.
    """
    rounded: list[dict] = []
    for p in points:
        q = dict(p)
        for key, places in (("x", 7), ("y", 7), ("z", 2), ("depth_m", 2)):
            v = q.get(key)
            if isinstance(v, float):
                q[key] = round(v, places)
        rounded.append(q)
    return rounded


def _build_chat_card_payloads(
    *,
    intent: str | None,
    tool_results: list[tuple[str, Any]],
):
    """Build ADR-0007 PR-1 chat-card payloads from structured tool results.

    Returns ``(map_payload, viz_payload)`` — either may be None.

    * ``project_summary`` → VizPayload(chart_type='technique_timeline').
      The frontend's TimelineCard reads:
        - plotly_layout.meta.swimlanes  (array of {technique, year_start,
          year_end, count, contractor, geologist})
        - plotly_layout.meta.breakdown_table (array of raw row dicts,
          used to render a tabular summary before the chart loads)
    * ``coverage_gap`` → VizPayload(chart_type='coverage_table') plus an
      optional MapPayload showing the project's collars colour-coded by
      whether they have any downstream data. The MapPayload is currently
      None — building a real GeoJSON requires re-querying collar
      geometries which the coverage tool doesn't return. PR-1 ships the
      table only; the spatial-holes map is a PR-2 follow-up.
    """
    from app.agent.tools import (  # noqa: PLC0415
        CollarDetailsResult,
        CoverageGapResult,
        DrillTrace3DResult,
        ProjectSummaryResult,
        StereonetResult,
    )
    from app.models.rag import MapPayload, VizPayload  # noqa: PLC0415

    map_payload = None
    viz_payload = None

    # ADR-0007 PR-4 — 3D drill-trace card. The execute_node keyword
    # trigger fires query_drill_traces_3d under any intent that surfaces
    # the matching keywords. Emit the card the moment a DrillTrace3DResult
    # shows up. Returns immediately so it takes precedence over the
    # intent-specific cards below (the user explicitly asked for the 3D
    # view).
    for _tool_name, result in tool_results:
        if isinstance(result, DrillTrace3DResult) and result.count > 0:
            drill_collars_meta = [
                {
                    "hole_id":     c.hole_id,
                    "collar_id":   c.collar_id,
                    "longitude":   c.longitude,
                    "latitude":    c.latitude,
                    "elevation":   c.elevation,
                    "total_depth": c.total_depth,
                    "hole_type":   c.hole_type,
                    "status":      c.status,
                    "azimuth":     c.azimuth,
                    "dip":         c.dip,
                    "trace_points": _round_trace_points_for_card(c.trace_points),
                }
                for c in result.collars
            ]
            intervals_meta = [
                {
                    "collar_id":     i.collar_id,
                    "depth_from":    i.depth_from,
                    "depth_to":      i.depth_to,
                    "interval_kind": i.interval_kind,
                    "color_hint":    i.color_hint,
                    "label":         i.label,
                    "source_row_id": i.source_row_id,
                }
                for i in result.intervals
            ]
            structures_meta = [
                {
                    "collar_id":      s.collar_id,
                    "depth":          s.depth,
                    "structure_type": s.structure_type,
                    "strike_deg":     s.strike_deg,
                    "dip_deg":        s.dip_deg,
                    "source_row_id":  s.source_row_id,
                }
                for s in result.structures
            ]
            title = (
                f"3D drill trace — {result.collars[0].hole_id}"
                if result.hole_id_filter and result.collars
                else f"3D drill traces — {result.count} hole(s)"
            )
            viz_payload = VizPayload(
                chart_type="drill_trace_3d",
                plotly_data=[],
                plotly_layout={
                    "meta": {
                        "collars":    drill_collars_meta,
                        "intervals":  intervals_meta,
                        "structures": structures_meta,
                        "project_id": result.project_id,
                        "hole_id_filter": result.hole_id_filter,
                    },
                },
                title=title,
            )
            return map_payload, viz_payload

    # Hole-detail card — when the factual_lookup short-circuit ran
    # query_collar_details and found a hole, emit a ``downhole_strip``
    # viz hint so the React StripLogViewer fetches lithology from
    # /api/v1/projects/{id}/collars/{collar_id}. The hint is intent-
    # agnostic (factual_lookup is the common driver) so it precedes the
    # intent-specific branches below.
    for _tool_name, result in tool_results:
        if (
            isinstance(result, CollarDetailsResult)
            and result.count == 1
            and result.hole_id
        ):
            viz_payload = VizPayload(
                chart_type="downhole_strip",
                plotly_data=[],
                plotly_layout={
                    "meta": {
                        "hole_id": result.hole_id,
                        "collar_id": result.collar_id,
                        "project_id": result.project_id,
                    },
                },
                title=f"Strip log — {result.hole_id}",
            )
            return map_payload, viz_payload

    # ADR-0007 PR-2 — stereonet card is intent-agnostic. The execute_node
    # keyword trigger fires query_stereonet under whatever intent the
    # classifier picked (most often synthesis); we surface the card the
    # moment a StereonetResult shows up in tool_results.
    for _tool_name, result in tool_results:
        if isinstance(result, StereonetResult) and result.count > 0:
            stereo_points = [
                {
                    "depth": p.depth,
                    "structure_type": p.structure_type,
                    "strike_deg": p.strike_deg,
                    "dip_deg": p.dip_deg,
                    "dip_direction_deg": p.dip_direction_deg,
                    "plunge_deg": p.plunge_deg,
                    "trend_deg": p.trend_deg,
                    "stereonet_x": p.stereonet_x,
                    "stereonet_y": p.stereonet_y,
                    "source_row_id": p.source_row_id,
                }
                for p in result.points
            ]
            viz_payload = VizPayload(
                chart_type="stereonet",
                plotly_data=[],
                plotly_layout={
                    "meta": {
                        "image_base64": result.image_base64,
                        "projection": result.projection,
                        "structure_count": result.count,
                        "points": stereo_points,
                        "project_id": result.project_id,
                    },
                },
                title="Stereonet — structural measurements",
            )
            # The stereonet card returns immediately so we don't waste cycles
            # under intents that have their own payload (project_summary /
            # coverage_gap); when both fire on the same query the stereonet
            # wins because it's the chart the user explicitly asked for.
            return map_payload, viz_payload

    if intent not in ("project_summary", "coverage_gap"):
        return None, None

    for _tool_name, result in tool_results:
        if (
            intent == "project_summary"
            and isinstance(result, ProjectSummaryResult)
            and result.technique_breakdown
        ):
            swimlanes: list[dict[str, Any]] = []
            breakdown_table: list[dict[str, Any]] = []
            for row in result.technique_breakdown:
                breakdown_table.append({
                    "technique": row.technique,
                    "source_table": row.source_table,
                    "year": row.year,
                    "count": row.count,
                    "total_metres": row.total_metres,
                    "contractor": row.contractor,
                    "geologist": row.geologist,
                    "source_row_ids": row.source_row_ids,
                })
                if row.year is not None:
                    swimlanes.append({
                        "technique": row.technique,
                        "year_start": row.year,
                        "year_end": row.year,
                        "count": row.count,
                        "contractor": row.contractor,
                        "geologist": row.geologist,
                        "source_table": row.source_table,
                    })

            viz_payload = VizPayload(
                chart_type="technique_timeline",
                plotly_data=[],
                plotly_layout={
                    "meta": {
                        "swimlanes": swimlanes,
                        "breakdown_table": breakdown_table,
                        "extraction_pending_fields": (
                            result.extraction_pending_fields
                        ),
                        "project_id": result.project_id,
                    },
                },
                title="Data collection breakdown",
            )
            break

        if (
            intent == "coverage_gap"
            and isinstance(result, CoverageGapResult)
            and (
                result.attribute_coverage
                or result.ingest_gap.indexed > 0
                or result.findings
            )
        ):
            rows = [
                {
                    "attribute": row.attribute,
                    "collars_with_data": row.collars_with_data,
                    "collars_total": row.collars_total,
                    "coverage_pct": row.coverage_pct,
                    "source_row_ids": row.source_row_ids,
                }
                for row in result.attribute_coverage
            ]
            ingest_block = {
                "indexed": result.ingest_gap.indexed,
                "processed": result.ingest_gap.processed,
                "gap_pct": result.ingest_gap.gap_pct,
            }
            findings_block = [
                {
                    "kind": f.kind,
                    "severity": f.severity,
                    "description": f.description,
                    "source_row_ids": f.source_row_ids,
                }
                for f in result.findings
            ]

            viz_payload = VizPayload(
                chart_type="coverage_table",
                plotly_data=[],
                plotly_layout={
                    "meta": {
                        "rows": rows,
                        "ingest_gap": ingest_block,
                        "findings": findings_block,
                        "project_id": result.project_id,
                    },
                },
                title="Coverage gap analysis",
            )

            # §6b P4 (2026-05-29) — the coverage_gap tool now returns a
            # real per-collar FeatureCollection in `result.gap_geojson`.
            # When it's populated, we ship the real map. When the tool
            # couldn't produce it (no geom_4326 column, DB error, project
            # with zero collars) we fall back to the PR-1 empty
            # FeatureCollection so the frontend still renders the
            # placeholder hint and the contract stays consistent.
            if rows:
                feature_count = 0
                if isinstance(result.gap_geojson, dict):
                    feature_count = len(result.gap_geojson.get("features", []))

                if feature_count > 0:
                    map_payload = MapPayload(
                        layer_id=f"coverage-gap-{result.project_id}",
                        layer_type="collar",
                        geojson=result.gap_geojson,
                        label=f"Coverage gaps — {feature_count} collar(s)",
                    )
                else:
                    # P4 fallback path. Indicates the project has no
                    # WGS84-located collars (geom_4326 NULL) or the
                    # tool query failed; preserves the disabled-map hint.
                    map_payload = MapPayload(
                        layer_id=f"coverage-gap-{result.project_id}",
                        layer_type="collar",
                        geojson={"type": "FeatureCollection", "features": []},
                        label="Coverage gaps (no collar geometries available)",
                    )
            break

    return map_payload, viz_payload


def _attach_envelope_notes_to_uncertainty(
    response,
    *,
    envelope_notes: list[str],
    unspecified_descriptions: list[str],
):
    """Merge envelope notes + unspecified-field descriptions into
    ``geo_answer.uncertainty.missing_or_conflicting``.

    Returns a new ``GeoRAGResponse`` (Pydantic model_copy) when changes
    apply; otherwise returns the input unchanged.

    Skipped silently when:
      * ``response.geo_answer`` is None (OIUR flag off / parse fallback)
      * ``geo_answer.uncertainty`` is a :class:`SectionEmpty` (partial-
        evidence answer with no interpretations to qualify)
    """
    from app.agent.schemas import UncertaintyBlock  # noqa: PLC0415

    if response.geo_answer is None:
        return response
    uncertainty = response.geo_answer.uncertainty
    if not isinstance(uncertainty, UncertaintyBlock):
        return response

    notes = list(envelope_notes) + [
        d for d in unspecified_descriptions if d not in envelope_notes
    ]
    if not notes:
        return response

    existing = list(uncertainty.missing_or_conflicting)
    merged = existing + [n for n in notes if n not in existing]
    new_uncertainty = uncertainty.model_copy(update={"missing_or_conflicting": merged})
    new_geo_answer = response.geo_answer.model_copy(
        update={"uncertainty": new_uncertainty}
    )
    return response.model_copy(update={"geo_answer": new_geo_answer})


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _floor_confidence_with_warning_banner(
    response: GeoRAGResponse, reason: str,
) -> GeoRAGResponse:
    """Floor ``response.confidence`` to <=0.2 and prepend a caveat banner.

    This is the single UX for "this answer must not ship looking like a
    normal, cleanly-validated answer" — shared by the should_retry=True
    guard-failure path and the fail-closed exception path in
    ``validate_node`` (2026-08-15 fix) so a genuine failed check and an
    exception that prevented checking from running at all look the same
    to the user: unverified, low-confidence, clearly flagged. Never
    raises — on any internal failure it logs and returns ``response``
    unchanged rather than risk losing the answer entirely.
    """
    try:
        floored = min(float(getattr(response, "confidence", 0.2) or 0.2), 0.2)
        banner = (
            "**Note: automated fact-checking flagged a potential issue "
            f"with this answer** ({reason}) — treat the following with "
            "caution and verify against the source documents directly.\n\n"
        )
        return response.model_copy(update={
            "confidence": floored,
            "text": banner + response.text,
        })
    except Exception:  # pragma: no cover — defensive
        logger.debug(
            "agentic_retrieval.validate: confidence floor/banner skipped",
            exc_info=True,
        )
        return response


#: What the banner tells a geologist about each guard, keyed by the layer
#: number that opens a validator warning ("Layer 3: ...", "Layer 4/6: ...").
#: The warnings themselves are written for operators — they name rule
#: numbers, internal layers and design docs — so they stay in
#: ``validation_warnings`` and the logs, and never reach the answer text.
_BANNER_REASON_BY_LAYER: dict[str, str] = {
    "1": "the documents found were only a weak match for the question",
    "2": "some statements had no supporting source and were removed",
    "3": "a number in the answer could not be matched to the source documents",
    "4": "a hole, project or report named in the answer could not be found in the project's records",
    "5": "a citation did not point to a document found for this question",
    "6": "a value in the answer failed a geological consistency check",
}
_BANNER_REASON_DEFAULT = "part of the answer could not be checked against the sources"
_LAYER_PREFIX = re.compile(r"^\s*Layer\s+(\d)")


def _banner_reason(warning: str | None) -> str:
    """Plain-language reason for the caveat banner, from a validator warning."""
    match = _LAYER_PREFIX.match(warning or "")
    if match is None:
        return _BANNER_REASON_DEFAULT
    return _BANNER_REASON_BY_LAYER.get(match.group(1), _BANNER_REASON_DEFAULT)


def _extract_conflicts_safely(text: str) -> list[dict[str, Any]] | None:
    """`extract_conflicting_evidence`, but never fatal to an answer.

    A parse failure must not cost the user their answer — the worst case is
    the status quo ante, an unpopulated field.
    """
    from app.agent.conflict_extraction import (  # noqa: PLC0415
        extract_conflicting_evidence,
    )

    try:
        return extract_conflicting_evidence(text or "")
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "agentic_retrieval.validate: conflicting-evidence parse failed"
        )
        return None


async def validate_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Run the Layer-1/2/3/4/5/6 post-assembly validation the legacy path uses.

    Audit 2026-06-27 (T3): two gaps fixed here.
      1. Layer 2 (typed-output repair) was never run on the agentic path — it
         is now invoked first (sync, never raises) so orphan markers, empty
         text, out-of-range confidence and empty grounding are caught.
      2. ``should_retry`` from the Layer 3/4/6 validator was previously
         discarded (``_should_retry``). The agentic graph has no LLM
         re-generation loop yet, so we cannot re-call the model here; instead
         we FLOOR the answer's confidence and surface a loud warning so a
         fabrication- or constraint-flagged answer can never ship at normal
         confidence. (Follow-up: a real validate→execute retry edge.)

    CLAUDE.md hard rule #5 audit (post Step 2.5): Layer 5 (chunk provenance)
    was defined in layer5_provenance.py but never called anywhere on the
    live agentic path — its only prior caller lived in the deleted legacy
    orchestrator body. Wired in here since 2026-08-21 as a pure enrichment
    pass (appends source-file provenance onto Citation.section, never
    rejects). As of 2026-09-24 a GATE half runs first (``gate_citation_provenance``)
    that DOES reject: any document-chunk citation whose chunk was not
    actually retrieved for this query, or carries no document id, is
    dropped — restoring the "provenance is a gate, not just enrichment"
    half of hard rule 5. Per hard rule 4 ("every claim must include a
    source_chunk_id or be rejected"), a rejection also removes the
    SENTENCE(S) that carried the dropped marker, not just the bracket
    text — a bare marker-strip would ship the claim it used to back as
    uncited prose. See ``gate_citation_provenance``'s own docstring for
    the sentence-removal rule and the all-rejected refusal fallback
    (rag-expert review, 2026-09-24).
    Layer 1's hard half (zero-evidence refusal) runs earlier still, in
    assemble_node, before the LLM is ever called; its advisory half
    (``verify_retrieval_quality``) runs inside
    ``run_post_assembly_validation`` below alongside Layers 3/4/6.

    Fail-closed exception posture (2026-08-15): ``run_post_assembly_validation``
    wraps numeric grounding (Layer 3), entity resolution, and geological
    constraint checking (Layer 4/6) in one call. Previously, ANY exception
    from ANY of those sub-checks (a DB timeout, a malformed tool-result
    shape, a bug in one guard) was caught here and discarded wholesale —
    the response shipped with ``validation_warnings=[]``, indistinguishable
    from an answer that passed every check cleanly, and ``should_retry``
    never fired so the confidence-floor/banner mechanism below never
    engaged either. That is a fail-OPEN posture on exactly the exception
    path meant to protect against fabrication. Per the "if any guard
    raises, treat as failed" posture the §04i guard contract specifies
    (documented in the deleted layer_completeness.py until 2026-08-21, and
    now carried by this comment), the except branch now fails CLOSED: it reuses the same confidence-floor + warning-banner UX
    as a genuine should_retry=True guard failure (via
    ``_floor_confidence_with_warning_banner``) and returns
    ``validation_warnings`` describing that validation could not run, so
    downstream consumers can tell "unverified" apart from "verified
    clean". This does not hard-refuse the answer — a transient DB blip
    shouldn't make a query unanswerable — it only ensures the answer is
    never silently presented as fully checked when it wasn't.
    """
    from app.agent.hallucination.layer2_typed_output import (  # noqa: PLC0415
        enforce_claim_citations,
        validate_and_repair,
        validate_and_repair_with_findings,
    )
    from app.agent.hallucination.layer5_provenance import (  # noqa: PLC0415
        enrich_provenance,
        gate_citation_provenance,
    )
    from app.agent.hallucination.orchestrator_validators import (  # noqa: PLC0415
        run_post_assembly_validation,
    )

    assert state.response is not None, "assemble_node must run before validate_node"

    async def _enrich_provenance_safely(resp: GeoRAGResponse) -> GeoRAGResponse:
        pg_pool = getattr(state.deps, "pg_pool", None)
        if pg_pool is None:
            return resp
        try:
            return await enrich_provenance(resp, pg_pool)
        except Exception:  # pragma: no cover — defensive, enrich_provenance already never raises
            logger.debug("agentic_retrieval.validate: layer5 enrichment failed", exc_info=True)
            return resp

    # Layer 2 — typed-output repair (sync, never raises). An invented
    # citation marker now takes its claim sentence with it and is a finding
    # (RAG-7), folded into should_retry below like a Layer 5 rejection.
    response, layer2_findings = validate_and_repair_with_findings(state.response)

    # Layer 5 (gate half), restored 2026-09-24 — reject citations whose
    # chunk was not actually retrieved for this query (or carries no
    # document id) BEFORE Layer 2 runs its second pass below. The gate
    # itself removes the sentence(s) that carried a rejected marker (hard
    # rule 4 — see gate_citation_provenance's docstring), so it normally
    # leaves no orphan marker behind for Layer 2 to find; the second
    # validate_and_repair pass below is kept as a cheap, idempotent
    # backstop for anything the gate's regex-based sentence split missed.
    # Wrapped in its own try/except even though the gate is pure/no-I/O —
    # a bug here must not cost the user their answer.
    layer5_gate_warnings: list[str] = []
    try:
        response, layer5_gate_warnings = gate_citation_provenance(
            response, state.tool_results
        )
    except Exception:
        # FAIL CLOSED (AGT-11). This used to log and carry on with the
        # citations unchecked and should_retry untouched, so an answer whose
        # provenance could not be verified shipped as "clean" — while the
        # Layer 3/4/6 exception path below already failed closed.
        logger.exception(
            "agentic_retrieval.validate: layer5 provenance GATE raised — "
            "failing CLOSED (answer marked unverified on provenance)"
        )
        layer5_gate_warnings = [
            "Layer 5: the chunk-provenance gate could not run — citations in "
            "this answer are UNVERIFIED against the retrieved chunks, not "
            "confirmed clean."
        ]
    else:
        if layer5_gate_warnings:
            response = validate_and_repair(response)

    # CLAUDE.md hard rule 4 — every claim carries a citation or is removed
    # (2026-09-29, RAG-7). Runs after the marker repairs above so it judges
    # the markers that survived, and before Layers 3/4/6 so they check the
    # text that will actually ship. Fails closed like the gate above.
    rule4_findings: list[str] = []
    try:
        response, rule4_findings = enforce_claim_citations(response)
    except Exception:
        logger.exception(
            "agentic_retrieval.validate: rule-4 citation enforcement raised — "
            "failing CLOSED"
        )
        rule4_findings = [
            "Layer 2: citation enforcement could not run — uncited claims in "
            "this answer were NOT removed; it is unverified, not confirmed clean."
        ]
    citation_findings = [*layer2_findings, *layer5_gate_warnings, *rule4_findings]

    try:
        response, warnings, should_retry = await run_post_assembly_validation(
            response, state.tool_results, state.deps
        )
    except Exception:
        logger.exception(
            "agentic_retrieval.validate: post-assembly validation raised — "
            "failing CLOSED: treating response as unverified (confidence "
            "floored, warning banner applied) instead of shipping it as "
            "cleanly validated"
        )
        _unverified_warning = (
            "Layer 3/4/6: post-assembly validation raised an exception "
            "before numeric grounding, entity resolution, and constraint "
            "checks could complete — this answer is UNVERIFIED, not "
            "confirmed clean."
        )
        response = _floor_confidence_with_warning_banner(
            response,
            "automated fact-checking could not complete due to an "
            "internal error",
        )
        response = await _enrich_provenance_safely(response)
        response = response.model_copy(update={"validation_state": "unverified"})
        return {
            "response": response,
            "validation_warnings": [*citation_findings, _unverified_warning],
        }

    # Layer 2 / Layer 5 / rule-4 findings are folded in here (not inside
    # run_post_assembly_validation, which only sees tool_results/text, not
    # the citations list) and always force should_retry — an invented or
    # rejected citation, or a claim removed for carrying none, is exactly
    # the "fabrication or a bug shipped a wrong source" case the other
    # guards' should_retry path exists for.
    warnings = [*citation_findings, *warnings]
    should_retry = should_retry or bool(citation_findings)

    if should_retry:
        warnings = [
            *warnings,
            "Layer 4/6: fabrication or constraint signal detected — answer "
            "not re-generated (agentic path has no retry loop); confidence "
            "floored.",
        ]
        # Confidence-flooring ALONE used to be the only signal — nothing
        # downstream (Laravel, FastAPI routers, the frontend) gates
        # delivery on GeoRAGResponse.confidence, so a should_retry=True
        # response (fabricated hole-ID/entity, an impossible geological
        # value, or ≥3 ungrounded numbers) shipped as an ordinary-looking
        # cited answer with only a quieter number attached. The agentic
        # path genuinely has no re-generation loop yet (that's the
        # separate, still-shadow-mode repair_shadow_node rollout — see
        # docs/architecture/repair_loop_spec.md), so this doesn't try to
        # fix or regenerate the answer; it makes the caveat impossible
        # to miss by putting it in the text itself, since that's the one
        # thing every consumer of this response actually renders.
        # Citations/markers are left untouched (the retrieved evidence
        # is real; what's unverified is the LLM's synthesis of it).
        # Found in a full-app review, 2026-08-05. Flooring + banner logic
        # now lives in ``_floor_confidence_with_warning_banner`` (shared
        # with the fail-closed exception path above, 2026-08-15) so a
        # genuine guard failure and a validation-couldn't-run exception
        # present identically to the user.
        _reason = _banner_reason(warnings[0] if warnings else None)
        response = _floor_confidence_with_warning_banner(response, _reason)
        logger.error(
            "agentic_retrieval.validate: should_retry=True — confidence "
            "floored to %.2f and warning banner prepended. warnings=%s",
            float(getattr(response, "confidence", 0.2) or 0.2),
            warnings,
        )

    # L909 — the state the UI actually branches on. `confidence` is a
    # retrieval-strength number (see GeoRAGResponse.confidence): a
    # structured-only hit scores 0.95 whatever the synthesis says, and the
    # frontend rendered 0.05 and 0.95 in the same neutral pill. This is the
    # signal that says whether anything checked the ANSWER.
    #
    # Any warning at all counts as flagged. The warnings that reach here are
    # guard findings — an ungrounded number, an entity that resolves to
    # nothing, a violated geological constraint — not chatter; `should_retry`
    # is the subset severe enough to also floor the confidence and prepend a
    # banner, but a single ungrounded number in an otherwise clean answer is
    # exactly the case the old UX lost entirely (below NUMERIC_RETRY_THRESHOLD,
    # so no banner, no floor, and demote_node is a no-op while
    # GEO_ANSWER_OIUR_ENABLED is False).
    response = await _enrich_provenance_safely(response)

    # L941 — read the model's "### Conflicting evidence" sub-section back into
    # the structured field. Until 2026-08-21 `conflicting_evidence` had no
    # production writer at all, so `conflicts_present` in apply_guard_demotion
    # was permanently False and the "conflicting sources force Low confidence"
    # rule could not fire however loudly the answer reported a disagreement.
    # See app/agent/conflict_extraction.py for what this is (a parser for the
    # model's report) and what it is not (a detector).
    conflicts = _extract_conflicts_safely(response.text)

    # Stamped last, after Layer 5 enrichment, so the object handed to
    # enrich_provenance is the one assemble_node built — several tests assert
    # that identity, and enrichment has no bearing on the verdict.
    update: dict[str, Any] = {
        "validation_state": "flagged" if warnings else "clean",
    }
    if conflicts:
        update["conflicting_evidence"] = conflicts
        logger.info(
            "agentic_retrieval.validate: %d conflicting-evidence row(s) "
            "parsed from the answer", len(conflicts),
        )
    response = response.model_copy(update=update)
    return {"response": response, "validation_warnings": warnings}


# ---------------------------------------------------------------------------
# demote
# ---------------------------------------------------------------------------


async def demote_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Apply Phase 1.3 Stage-2 confidence demotion using L3 warnings + conflicts."""
    from app.agent.confidence_computer import apply_guard_demotion  # noqa: PLC0415

    assert state.response is not None, "validate_node must run before demote_node"
    response, reasons = apply_guard_demotion(state.response, state.validation_warnings)
    if reasons:
        logger.info(
            "agentic_retrieval.demote: confidence demotion applied: %s",
            "; ".join(reasons),
        )
    return {"response": response, "demotion_reasons": reasons}


# ---------------------------------------------------------------------------
# repair_shadow (Plan §4b/§4c Stage 1 — telemetry only)
# ---------------------------------------------------------------------------


async def repair_shadow_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Shadow-mode repair loop pass (Plan §4b Stage 1).

    Sits between ``demote_node`` and ``persist_node``. When
    ``settings.REPAIR_LOOP_SHADOW_ENABLED`` is True:

      1. Calls :func:`app.agent.guards.classify_guards` to derive the
         typed :class:`GuardErrorCode` list from current state.
      2. Calls :func:`app.agent.repair_strategy.plan_repair` to derive
         the :class:`RepairPlan` the orchestrator WOULD attempt next.
      3. Writes ``state.repair_codes_observed`` (the codes),
         ``state.repair_strategy_history`` (the strategies that would
         fire), and ``state.repair_terminal_reason`` (when terminal).

    In shadow mode (the only mode enabled by default) it does NOT:

      - Modify ``state.response``, ``state.retrieval_profile``, or
        ``state.retrieval_filters``.
      - Re-issue any retrieval node.
      - Bump ``state.repair_attempts`` (no actual attempt happened —
        the full loop is what records attempts).

    With REPAIR_LOOP_TERMINAL_ENABLED it stamps ``refusal_payload`` for a
    terminal plan. With REPAIR_LOOP_LOWCOST_ENABLED / _FULL_ENABLED it may
    REPLACE the response — but only with a re-issue that has itself been
    through validate_node and demote_node (``_revalidate_repaired_answer``)
    and was never streamed; if that re-validation fails, the attempt is
    discarded (audit AGT-3 / RAG-19, 2026-09-29). All three flags default
    False.

    When the flag is off (default), the node is a no-op pass-through.
    This means the graph wiring can land NOW without changing behaviour,
    and the flag flip at deploy time is the only change that matters
    for the rollout.

    Best-effort: a failure inside classify / plan_repair logs and
    returns no state update — the answer path is untouched.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.REPAIR_LOOP_SHADOW_ENABLED:
        return {}

    try:
        from app.agent.guards import (  # noqa: PLC0415
            GuardErrorCode,
            classify_guards,
        )
        from app.agent.repair_strategy import (  # noqa: PLC0415
            plan_repair,
        )

        # Conflict signal — same heuristic the persist node uses, kept
        # in sync so the shadow's code list matches what persist writes.
        conflicting = bool(
            state.response is not None
            and getattr(state.response, "conflicting_evidence", None)
        )
        citations = (
            list(state.response.citations)
            if state.response is not None and state.response.citations
            else []
        )
        citation_state = (
            "rejected" if state.response is not None and not citations else "committed"
        )

        codes: list[GuardErrorCode] = classify_guards(
            validation_warnings=list(state.validation_warnings),
            demotion_reasons=list(state.demotion_reasons),
            tool_results=list(state.tool_results),
            response_citations=citations,
            citation_lifecycle_state=citation_state,
            conflicting_evidence_present=conflicting,
        )
        codes_str = [c.value for c in codes]

        # Shadow mode treats this as the FIRST attempt (prior_strategies
        # empty). When the full loop lands, the prior list comes from
        # state.repair_strategy_history accumulated across iterations.
        plan = plan_repair(codes, max_attempts=2, prior_strategies=())

        # Stamp telemetry onto state for the trace.
        strategies_str = [s.value for s in plan.strategies]

        logger.info(
            "agentic_retrieval.repair_shadow: codes=%s strategies=%s terminal=%s "
            "reason=%s (no state mutation)",
            codes_str,
            strategies_str,
            plan.terminal,
            plan.reason,
        )

        update_dict: dict[str, Any] = {
            "repair_codes_observed": codes_str,
            "repair_strategy_history": strategies_str,
            "repair_terminal_reason": plan.reason,
        }

        # Plan §4b Stage 2 — terminal-strategy stamping. When the
        # dispatcher picked a terminal strategy AND the Stage 2 flag
        # is on, stamp response.refusal_payload so the frontend's
        # GuardErrorDispatcher routes to the right surface (Refusal /
        # AmbiguityPicker / UnitPicker / DepthPicker / ConflictSideBySide).
        # No-op when Stage 2 is off or the plan isn't terminal.
        if (
            _settings.REPAIR_LOOP_TERMINAL_ENABLED
            and plan.terminal
            and plan.strategies
            and state.response is not None
        ):
            terminal_payload = _build_terminal_refusal_payload(
                state, plan.strategies[0], codes,
            )
            if terminal_payload is not None:
                try:
                    state.response.refusal_payload = terminal_payload
                    update_dict["response"] = state.response
                    logger.info(
                        "agentic_retrieval.repair_stage2: stamped refusal_payload "
                        "(strategy=%s, reason_code=%s)",
                        plan.strategies[0].value,
                        terminal_payload.get("reason_code"),
                    )
                except Exception:  # pragma: no cover — defensive
                    logger.debug(
                        "repair_stage2: refusal_payload stamp failed",
                        exc_info=True,
                    )

        # Plan §4b Stages 3 + 4 — actual loop iteration. When the
        # LOWCOST or FULL flag is on AND the plan has a loop-friendly
        # strategy AND we haven't hit max_attempts, apply the strategy
        # and re-issue the relevant sub-pipeline. The loop driver runs
        # INSIDE this node (not as a LangGraph cycle) so the graph
        # topology stays a DAG.
        stages_active = (
            _settings.REPAIR_LOOP_LOWCOST_ENABLED
            or _settings.REPAIR_LOOP_FULL_ENABLED
        )
        if (
            stages_active
            and plan.strategies
            and not plan.terminal
            and state.response is not None
        ):
            try:
                loop_update = await _run_repair_loop(state, plan)
                update_dict.update(loop_update)
            except Exception:  # pragma: no cover — defensive
                logger.exception(
                    "agentic_retrieval.repair_loop: failed (non-fatal, "
                    "answer path untouched)"
                )

        # Mutate state with the freshly-derived codes — LangGraph hasn't
        # merged update_dict yet, and later reads expect the live value.
        state.repair_codes_observed = codes_str

        # The repair loop re-issues the LLM (`_reissue_llm_only`), and
        # those retries are billed like any other call.
        update_dict.update(_fold_token_usage(state))
        return update_dict
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "agentic_retrieval.repair_shadow: failed (non-fatal, telemetry only)"
        )
        return {}


# ---------------------------------------------------------------------------
# Plan §4b Stages 3 + 4 — repair loop driver
# ---------------------------------------------------------------------------


async def _run_repair_loop(
    state: AgenticRetrievalState,
    initial_plan: Any,  # RepairPlan
) -> dict[str, Any]:
    """Iterate the repair loop up to REPAIR_LOOP_MAX_ATTEMPTS times.

    Each iteration:
      1. Pick the next strategy from the (possibly updated) plan
      2. Stage 3 path (LLM-only): apply_llm_only_strategy → re-call
         _call_llm with the suffix appended → update state.response
      3. Stage 4 path (retrieval-side): apply_retrieval_strategy →
         merge mutations into state.retrieval_filters / .retrieval_profile
         → re-run execute_node → re-run assemble_node
      4. Record a RepairAttempt; check detect_death_loop
      5. Re-classify guards + re-plan; if no codes OR plan terminal
         OR max_attempts reached → exit

    Returns the state-merge dict (response, retrieval_filters,
    retrieval_profile, repair_attempts, tool_results, evidence_packet,
    etc.) — whatever changed.

    Pure-async; never raises (caller wraps in try/except too).
    """
    import time as _time_mod  # noqa: PLC0415

    from app.agent.guards import (  # noqa: PLC0415
        RepairAttempt,
        classify_guards,
        detect_death_loop,
    )
    from app.agent.repair_apply import (  # noqa: PLC0415
        apply_llm_only_strategy,
        apply_retrieval_strategy,
    )
    from app.agent.repair_strategy import (  # noqa: PLC0415
        TERMINAL_STRATEGIES,
        RepairStrategy,
        plan_repair,
    )
    from app.config import settings as _settings  # noqa: PLC0415

    max_attempts = max(0, int(getattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 2)))
    if max_attempts == 0:
        return {}

    attempts: list[RepairAttempt] = list(state.repair_attempts)
    history_str: list[str] = list(state.repair_strategy_history)
    current_plan = initial_plan
    death_loop_triggered = False

    for _iter in range(max_attempts):
        # Choose the next strategy to apply.
        next_strategy: RepairStrategy | None = None
        for s in current_plan.strategies:
            if s in TERMINAL_STRATEGIES:
                continue  # terminal handled by Stage 2 stamping
            if s.value in history_str:
                continue  # don't re-apply the same strategy
            next_strategy = s
            break

        if next_strategy is None:
            break

        applied = False
        # Everything a re-issue can touch, so a failed or rejected attempt
        # leaves the validated answer exactly as validate/demote left it.
        before = _repair_checkpoint(state)

        # Stage 3 path — LLM-only retry.
        suffix = apply_llm_only_strategy(next_strategy)
        if suffix is not None and _settings.REPAIR_LOOP_LOWCOST_ENABLED:
            try:
                await _reissue_llm_only(state, suffix)
                applied = True
                logger.info(
                    "agentic_retrieval.repair_loop: Stage 3 — applied %s (LLM-only re-issue)",
                    next_strategy.value,
                )
            except Exception:
                _restore_repair_checkpoint(state, before)
                logger.warning(
                    "repair_loop: LLM-only re-issue failed for %s",
                    next_strategy.value,
                    exc_info=True,
                )

        # Stage 4 path — retrieval-side. Only fires when the strategy
        # is NOT Stage-3-eligible AND the FULL flag is on.
        if not applied and _settings.REPAIR_LOOP_FULL_ENABLED:
            snapshot = {
                "retrieval_profile": _snapshot_field(state.retrieval_profile),
                "retrieval_filters": _snapshot_field(state.retrieval_filters),
                "context_envelope": _snapshot_field(state.context_envelope),
            }
            mutations = apply_retrieval_strategy(next_strategy, snapshot)
            if mutations:
                try:
                    await _reissue_retrieval(state, mutations)
                    applied = True
                    logger.info(
                        "agentic_retrieval.repair_loop: Stage 4 — applied %s "
                        "(retrieval + assemble re-issued)",
                        next_strategy.value,
                    )
                except Exception:
                    _restore_repair_checkpoint(state, before)
                    logger.warning(
                        "repair_loop: retrieval re-issue failed for %s",
                        next_strategy.value,
                        exc_info=True,
                    )

        if not applied:
            # Neither stage's flag matched this strategy. Skip and exit
            # — the strategy isn't actionable under the current flag set.
            logger.info(
                "repair_loop: strategy %s not actionable under current flags; "
                "exiting loop",
                next_strategy.value,
            )
            break

        # Audit AGT-3 / RAG-19: a re-issued answer goes through the SAME
        # validate -> demote chain the first answer did before it may
        # replace it — Layer 2 marker repair, the Layer 5 provenance gate,
        # Layers 3/4/6, the confidence floor + banner, demotion. It used to
        # be swapped in straight from assemble_response, and the loop's
        # re-classification below read the STALE validation_warnings, so it
        # could declare "no codes fire" about text nothing had checked. If
        # validation itself fails, the attempt is discarded: the answer
        # that already passed the guards is what ships.
        try:
            await _revalidate_repaired_answer(state)
        except Exception:
            _restore_repair_checkpoint(state, before)
            logger.exception(
                "repair_loop: re-validation of the %s re-issue failed — "
                "discarding it and keeping the validated answer",
                next_strategy.value,
            )
            break

        # Record the attempt.
        history_str.append(next_strategy.value)
        attempts.append(
            RepairAttempt(
                tool_name=(
                    state.tool_results[0][0]
                    if state.tool_results else "(none)"
                ),
                filters={},  # snapshot deliberately empty — too verbose for the dataclass dict[str, primitive] type
                result_count=len(state.tool_results),
                attempted_at_monotonic=_time_mod.monotonic(),
            )
        )

        # Death-loop check.
        if detect_death_loop(attempts):
            death_loop_triggered = True
            logger.warning(
                "repair_loop: detect_death_loop fired after attempt %d — exiting",
                len(attempts),
            )
            break

        # Re-classify + re-plan for the next iteration.
        try:
            new_codes = classify_guards(
                validation_warnings=list(state.validation_warnings),
                demotion_reasons=list(state.demotion_reasons),
                tool_results=list(state.tool_results),
                response_citations=(
                    list(state.response.citations) if state.response else []
                ),
                citation_lifecycle_state=(
                    "rejected"
                    if state.response is None or not state.response.citations
                    else "committed"
                ),
                conflicting_evidence_present=bool(
                    state.response and getattr(state.response, "conflicting_evidence", None)
                ),
            )
            if not new_codes:
                logger.info(
                    "repair_loop: no codes fire after attempt %d — exit clean",
                    len(attempts),
                )
                break
            current_plan = plan_repair(
                new_codes,
                max_attempts=max_attempts,
                prior_strategies=history_str,
            )
            if current_plan.terminal:
                # Stamp the terminal payload too (Stage 2 stamping inside
                # the loop). The original update_dict caller already
                # stamped the first plan's terminal; this catches a
                # terminal that DEVELOPS on an iteration.
                logger.info(
                    "repair_loop: terminal plan reached on iter %d (%s)",
                    len(attempts), current_plan.reason,
                )
                break
        except Exception:  # pragma: no cover — defensive
            logger.exception("repair_loop: re-classify failed; exiting")
            break

    if not attempts:
        return {}

    return {
        # Only ever a response that has been through validate + demote
        # (see _revalidate_repaired_answer) — persist and the `completed`
        # frame never see an unchecked re-issue.
        "response": state.response,
        "validation_warnings": list(state.validation_warnings),
        "demotion_reasons": list(state.demotion_reasons),
        "tool_results": list(state.tool_results),
        "evidence_packet": state.evidence_packet,
        "retrieval_filters": state.retrieval_filters,
        "retrieval_profile": state.retrieval_profile,
        "repair_attempts": attempts,
        "repair_strategy_history": history_str,
        "repair_terminal_reason": (
            "death loop detected" if death_loop_triggered
            else "loop completed"
        ),
    }


_REPAIR_CHECKPOINT_FIELDS = (
    "response",
    "validation_warnings",
    "demotion_reasons",
    "tool_results",
    "evidence_packet",
    "retrieval_filters",
    "retrieval_profile",
)


def _repair_checkpoint(state: AgenticRetrievalState) -> dict[str, Any]:
    return {name: getattr(state, name) for name in _REPAIR_CHECKPOINT_FIELDS}


def _restore_repair_checkpoint(
    state: AgenticRetrievalState, checkpoint: dict[str, Any],
) -> None:
    for name, value in checkpoint.items():
        setattr(state, name, value)


async def _revalidate_repaired_answer(state: AgenticRetrievalState) -> None:
    """Run validate_node then demote_node on a re-issued answer, in place.

    The repair loop runs inside repair_shadow_node, AFTER the graph's own
    validate and demote nodes, so a re-issue has to be pushed through the
    same two nodes explicitly. Raises if either does; the caller then
    discards the attempt.
    """
    validated = await validate_node(state)
    state.response = validated["response"]
    state.validation_warnings = list(validated.get("validation_warnings") or [])
    demoted = await demote_node(state)
    state.response = demoted["response"]
    state.demotion_reasons = list(demoted.get("demotion_reasons") or [])


def _snapshot_field(obj: Any) -> dict[str, Any]:
    """Coerce a Pydantic / dataclass / plain object into a dict for
    the strategy appliers. None → empty dict."""
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(exclude_none=False)
        except Exception:
            return {}
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}


async def _reissue_llm_only(
    state: AgenticRetrievalState, suffix: str,
) -> None:
    """Stage 3 — re-call the LLM with the repair-instruction suffix
    appended to the current system prompt. Updates state.response in
    place. Raises on LLM failure (caller wraps)."""
    from app.agent.llm_calls import _call_llm  # noqa: PLC0415
    from app.agent.orchestrator import _select_system_prompt  # noqa: PLC0415
    from app.agent.response_assembler import assemble_response  # noqa: PLC0415

    # Rebuild the context block + system prompt the same way
    # assemble_node would, then append the repair suffix.
    context_block, rendered_results = _render_context_and_evidence(
        state.tool_results,
        query=state.query,
        workspace_id=getattr(state.deps, "workspace_id", None),
    )

    # Same variant the first attempt used — a repair pass that switched
    # prompt variants mid-loop would be repairing against different rules
    # than the answer it is repairing.
    system_prompt = _select_system_prompt(
        categories=_categories_from_tool_results(rendered_results),
        query=state.query,
    ) + suffix

    openai_client = getattr(state.deps, "openai_http_client", None)
    anthropic_client = getattr(state.deps, "anthropic_client", None)

    # No token_callback: this answer is not streamed. It must pass
    # validate + demote (_revalidate_repaired_answer) before it can replace
    # the one already on screen, and only the `completed` frame carries it.
    text = await _call_llm(
        query=_question_for_llm(state),
        context=context_block,
        temperature=0.1,
        anthropic_client=anthropic_client,
        openai_http_client=openai_client,
        system_prompt=system_prompt,
        audit_label="agentic_retrieval_repair_stage3",
        workspace_id=getattr(state.deps, "workspace_id", None),
        redis_client=getattr(state.deps, "redis_client", None),
        pg_pool=getattr(state.deps, "pg_pool", None),
    )

    # Same card payloads and envelope notes assemble_node attaches — a
    # Stage 3 answer used to lose its map/viz cards and OIUR notes.
    map_payload, viz_payload = _build_chat_card_payloads(
        intent=state.effective_intent or state.intent,
        tool_results=state.tool_results,
    )
    new_response = assemble_response(
        text, rendered_results, map_payload=map_payload, viz_payload=viz_payload,
    )
    new_response = _attach_envelope_notes_to_uncertainty(
        new_response,
        envelope_notes=state.envelope_notes,
        unspecified_descriptions=unspecified_field_descriptions(state.context_envelope),
    )
    state.tool_results = rendered_results
    state.response = _with_retrieval_failures(new_response, state.retrieval_failures)


async def _reissue_retrieval(
    state: AgenticRetrievalState,
    mutations: dict[str, Any],
) -> None:
    """Stage 4 — merge the strategy's state mutations + re-run
    execute_node + assemble_node. Caller wraps in try/except.

    The mutations dict is a `model_copy(update=...)` payload for the
    matching state field (retrieval_profile / retrieval_filters).
    """
    # Apply mutations.
    if "retrieval_filters" in mutations and state.retrieval_filters is not None:
        try:
            state.retrieval_filters = state.retrieval_filters.model_copy(
                update=mutations["retrieval_filters"]
            )
        except Exception:
            logger.debug(
                "repair_loop: retrieval_filters model_copy failed",
                exc_info=True,
            )

    if "retrieval_profile" in mutations and state.retrieval_profile is not None:
        try:
            state.retrieval_profile = state.retrieval_profile.model_copy(
                update=mutations["retrieval_profile"]
            )
        except Exception:
            logger.debug(
                "repair_loop: retrieval_profile model_copy failed",
                exc_info=True,
            )

    # Re-run execute then assemble on a copy with the SSE callbacks removed:
    # assemble_node forwards token_callback into _call_llm, and a second
    # full answer used to stream into the same delta stream after the
    # first (audit AGT-3). The re-issue is validated before it can replace
    # anything; it is never streamed.
    silent = state.model_copy(update={
        "token_callback": None, "status_callback": None,
    })
    exec_update = await execute_node(silent)
    for name in ("tool_results", "evidence_packet"):
        if name in exec_update:
            setattr(silent, name, exec_update[name])
            setattr(state, name, exec_update[name])

    asm_update = await assemble_node(silent)
    for name in ("response", "tool_results", "evidence_packet"):
        if name in asm_update:
            setattr(state, name, asm_update[name])


#: The refusal panel shows ``message`` to the geologist, so it says what to
#: do next in plain terms. Keyed by RepairStrategy value; the strategy and
#: guard codes travel in their own fields for routing and audit.
_TERMINAL_REFUSAL_MESSAGES: dict[str, str] = {
    "ASK_FOR_DISAMBIGUATION": (
        "The question matches more than one hole, project or report. "
        "Name the one you mean and ask again."
    ),
    "REQUEST_UNIT_CLARIFICATION": (
        "The assay units are missing or unclear in the sources. Say which "
        "units you want (for example g/t or %) and ask again."
    ),
    "REQUEST_DEPTH_CLARIFICATION": (
        "The question needs a depth interval. Give the from and to depths "
        "and ask again."
    ),
    "SURFACE_CONFLICT": (
        "The sources disagree on this. Compare them side by side before "
        "relying on either."
    ),
    "REFUSE_OUT_OF_SCOPE": (
        "This question is outside what the project's data can answer."
    ),
}
_TERMINAL_REFUSAL_MESSAGE_DEFAULT = (
    "This question could not be answered from the project's sources."
)


def _build_terminal_refusal_payload(
    state: AgenticRetrievalState,
    strategy: Any,  # RepairStrategy — Any to avoid forward import
    codes: list[Any],  # list[GuardErrorCode]
) -> dict[str, Any] | None:
    """Build the structured refusal_payload for a terminal strategy.

    Plan §4b Stage 2 — drives the React GuardErrorDispatcher routing.
    Shape mirrors what RefusalBanner / AmbiguityPicker / UnitPickerCard
    / DepthPickerCard / ConflictSideBySide expect:

      {
        "type": "refusal",
        "reason_code": "MISSING_ASSAY_UNITS" / "AMBIGUOUS_HOLE_ID" / ...,
        "strategy": "REQUEST_UNIT_CLARIFICATION" / ...,
        "message": str,             # short user-facing summary
        "candidates": list[str],    # for picker surfaces
        "guard_codes": list[str],   # all codes that fired
      }

    Returns None when the strategy isn't terminal or no codes fired
    (defensive — shouldn't happen given the caller guard).
    """
    from app.agent.guards import GuardErrorCode  # noqa: PLC0415
    from app.agent.repair_strategy import TERMINAL_STRATEGIES, RepairStrategy  # noqa: PLC0415

    if strategy not in TERMINAL_STRATEGIES:
        return None

    code_values = [c.value if hasattr(c, "value") else str(c) for c in codes]
    # Pick the most-relevant code as reason_code:
    #   - For ASK_FOR_DISAMBIGUATION: the matching AMBIGUOUS_* code
    #   - For REQUEST_*_CLARIFICATION: the MISSING_* code
    #   - For SURFACE_CONFLICT: CONFLICTING_SOURCES
    #   - For REFUSE_OUT_OF_SCOPE: SOURCE_SCOPE_VIOLATION or UNSUPPORTED_QUERY_TYPE
    primary_code: str | None = None
    if strategy == RepairStrategy.ASK_FOR_DISAMBIGUATION:
        for c in code_values:
            if c.startswith("AMBIGUOUS_"):
                primary_code = c
                break
    elif strategy == RepairStrategy.REQUEST_UNIT_CLARIFICATION:
        primary_code = GuardErrorCode.MISSING_ASSAY_UNITS.value
    elif strategy == RepairStrategy.REQUEST_DEPTH_CLARIFICATION:
        primary_code = GuardErrorCode.MISSING_DEPTH_INTERVAL.value
    elif strategy == RepairStrategy.SURFACE_CONFLICT:
        primary_code = GuardErrorCode.CONFLICTING_SOURCES.value
    elif strategy == RepairStrategy.REFUSE_OUT_OF_SCOPE:
        for c in code_values:
            if c in (
                GuardErrorCode.SOURCE_SCOPE_VIOLATION.value,
                GuardErrorCode.UNSUPPORTED_QUERY_TYPE.value,
            ):
                primary_code = c
                break

    if primary_code is None:
        # Fall back to whichever code was first in the list — at least
        # the renderer has something specific to route on.
        primary_code = code_values[0] if code_values else "UNKNOWN_GUARD"

    return {
        "type": "refusal",
        "reason_code": primary_code,
        "strategy": strategy.value,
        "message": _TERMINAL_REFUSAL_MESSAGES.get(
            strategy.value, _TERMINAL_REFUSAL_MESSAGE_DEFAULT,
        ),
        "candidates": [],
        "guard_codes": code_values,
    }


# ---------------------------------------------------------------------------
# persist — §04i guard outcomes + refusal reason on the answer_runs row
# ---------------------------------------------------------------------------
#
# Added 2026-09-07. `rejection_reason` and `hallucination_guard_results`
# had been in the schema since 2026-04-22 / 2026-05-20 and nothing wrote
# them, so the `answer_quality_watch` refusal-rate and guard-fire signals,
# the Trust Inspector accepted/rejected split and the refusal-rate runbook
# all read zero. persist_node now classifies the guards once, before the
# INSERT, and writes both columns; the trace block reuses the same codes.


# answer_runs.reranker_version, the record of which reranker scored a run.
# The column has existed since 2026-04-21 and nothing wrote it, while
# app/services/reranker.py described it as the way a Rerank v4-scored run
# and a 3.5-scored run stay distinguishable after the fact, which is what
# re-measuring RERANKER_SCORE_THRESHOLD_HOSTED from traffic needs
# (ops/validation/rerank_threshold_probe.py --harvest-since).
#
# What was USED, not what is configured: a run whose every document search
# fell back to RRF order records "degraded:rrf", because its scores are
# fusion ranks, not reranker scores, and must not be read as either
# version. A run with no document search records NULL.
_RERANK_DEGRADED_VERSION = "degraded:rrf"


def _reranker_version_for_run(state: AgenticRetrievalState) -> str | None:
    """The reranker that actually scored this run's document chunks."""
    try:
        from app.agent.tools import DocumentSearchResult  # noqa: PLC0415
        from app.services.reranker import active_reranker_version  # noqa: PLC0415

        reranked = degraded = False
        for _name, result in state.tool_results or []:
            if isinstance(result, DocumentSearchResult) and result.chunks:
                if result.rerank_degraded:
                    degraded = True
                else:
                    reranked = True
        if reranked:
            return active_reranker_version()[:64]
        if degraded:
            return _RERANK_DEGRADED_VERSION
        return None
    except Exception:  # noqa: BLE001
        # Observability must never fail the persist.
        logger.debug("agentic_retrieval.persist: reranker_version unavailable", exc_info=True)
        return None


def _classify_persist_guards(
    state: AgenticRetrievalState, citation_state: str,
) -> list[Any]:
    """Run the §4b typed guard classifier for the persist step.

    Never raises: a classifier failure yields an empty list so the
    answer_runs row is still written.
    """
    try:
        from app.agent.guards import classify_guards  # noqa: PLC0415
        from app.agent.hallucination.citation_markers import (  # noqa: PLC0415
            CITATION_MARKER_RE as _CITATION_MARKER_RE,
        )

        _conflicting = bool(
            state.response is not None
            and getattr(state.response, "conflicting_evidence", None)
        )
        return list(
            classify_guards(
                validation_warnings=list(state.validation_warnings),
                demotion_reasons=list(state.demotion_reasons),
                tool_results=list(state.tool_results),
                response_citations=(
                    list(state.response.citations) if state.response is not None else []
                ),
                citation_lifecycle_state=citation_state,
                conflicting_evidence_present=_conflicting,
                # Does the answer actually cite anything, or does it just
                # come with citations attached? The assembler used to
                # guarantee the two matched by stapling every marker onto
                # the last sentence, which made the difference invisible.
                text_has_markers=(
                    bool(_CITATION_MARKER_RE.search(state.response.text))
                    if state.response is not None
                    else None
                ),
            )
        )
    except Exception:  # noqa: BLE001 — persistence must not depend on the classifier
        logger.warning(
            "agentic_retrieval.persist: guard classification failed — "
            "writing an empty guard envelope",
            exc_info=True,
        )
        return []


def _build_guard_results(guard_failure_codes: list[str]) -> dict[str, Any]:
    """Envelope for ``silver.answer_runs.hallucination_guard_results``.

    Shape per migration 2026_05_20_020000: ``schema_version`` / ``guards``
    / ``captured_at``. ``guards`` is keyed by :class:`GuardErrorCode`
    value; an empty object means the chain ran and nothing fired. NULL
    (never written here) means the chain did not run.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    guards: dict[str, dict[str, str]] = {}
    for code in guard_failure_codes:
        guards[code] = {"status": "notice" if code == "CONFLICTING_SOURCES" else "fail"}
    return {
        "schema_version": 1,
        "guards": guards,
        "captured_at": datetime.now(UTC).isoformat(),
    }


def _build_rejection_reason(
    state: AgenticRetrievalState,
    citation_state: str,
    guard_failure_codes: list[str],
) -> str | None:
    """Structured reason for ``silver.answer_runs.rejection_reason``.

    NULL unless the run is a refusal — ``citation_lifecycle_state =
    'rejected'`` (no real citations survived) or a terminal repair
    strategy stamped a ``refusal_payload`` on the response, which is what
    the client renders as *rejected*. The leading token is the code the
    runbook groups on: the payload's ``reason_code`` when there is one,
    ``insufficient_evidence`` (RefusalReasonCode) for a citation-less
    run, else the first guard code. The remaining guard codes follow in
    parentheses so nothing the classifier found is lost.
    """
    payload = getattr(state.response, "refusal_payload", None) if state.response else None
    payload = payload if isinstance(payload, dict) else None
    if citation_state != "rejected" and not payload:
        return None

    primary: str | None = None
    if payload:
        raw = payload.get("reason_code")
        primary = str(raw) if raw else None
    if not primary and citation_state == "rejected":
        primary = "insufficient_evidence"
    if not primary:
        primary = guard_failure_codes[0] if guard_failure_codes else "UNKNOWN_GUARD"

    others = [c for c in guard_failure_codes if c != primary]
    if not others:
        return primary
    return f"{primary} (guards: {', '.join(others)})"


# ---------------------------------------------------------------------------
# persist (Phase 4 follow-up — closes the lineage gap the smoke test exposed)
# ---------------------------------------------------------------------------


async def _write_chat_usage_event(
    pg_pool: Any,
    *,
    workspace_id: str | None,
    model_id: str | None,
    backend: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int | None,
    trace_id: str | None,
    answer_run_id: str | None,
) -> None:
    """Record one `usage.usage_events` row for an answered chat query.

    `usage.usage_events` had **0 rows** in production on 2026-08-21. The
    only writer was `agents/wrapper.py`'s `@georag_agent` decorator, which
    covers the phase0 ops agents and not the chat path — the path that
    spends the money. `cost_burn_watcher` sums this table every 5 minutes
    to decide whether to emit `cost.burn.alert` and, at 2x the hourly
    ceiling, to call `_suspend_workspace`. With no rows, every workspace
    summed to $0 and neither branch could ever run.

    On `projected_cost_usd`: it is 0 unless the model has a published rate
    in `agent/pricing.py`. Production runs `Cohere-command-a-plus-05-2026`,
    which has no entry, so `estimate_cost_usd` would have returned
    STANDARD-tier Sonnet pricing — a number with no relationship to the
    invoice. Recording 0 keeps the token counts (which ARE facts) while
    leaving the cost column empty, and `cost_burn_watcher`'s
    `HAVING SUM(projected_cost_usd) > 0` skips the workspace rather than
    suspending it over an invented figure. Add the rate to `_PRICE_TABLE`
    and both the alert and the hard stop start working, with no change
    here.

    Best-effort: the answer has already been streamed to the user by the
    time this runs, so a failed write is logged and swallowed.
    """
    from app.agent.pricing import estimate_cost_usd, has_pricing  # noqa: PLC0415

    cost_usd = 0.0
    if model_id and has_pricing(model_id):
        cost_usd = estimate_cost_usd(
            model=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    try:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO usage.usage_events
                    (workspace_id, agent_name, agent_version, model_profile,
                     model_id, tokens_prompt, tokens_completion,
                     projected_cost_usd, latency_ms, outcome, trace_id,
                     invocation_id)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::uuid)
                """,
                workspace_id,
                "chat_rag",
                CHAT_USAGE_AGENT_VERSION,
                backend,
                model_id,
                int(input_tokens),
                int(output_tokens),
                cost_usd,
                latency_ms,
                "success",
                trace_id,
                answer_run_id,
            )
    except Exception:
        logger.warning(
            "agentic_retrieval.persist: usage_events INSERT failed — this "
            "query's spend is unaccounted for and cost_burn_watcher will "
            "not see it",
            exc_info=True,
            extra={"workspace_id": workspace_id, "trace_id": trace_id},
        )


#: Whole-persist wall-clock budget (audit AGT-6). persist_node sits on the
#: path to the `completed` SSE frame — routers/queries.py queues `done` only
#: after the graph returns — so an unbounded pool wait or retry ladder here
#: can turn a fully streamed answer into a TIMEOUT frame. Past this budget
#: the lineage row is given up (logged + counted), not the answer.
_PERSIST_BUDGET_S = 5.0
#: Separate, smaller budget for the usage metering INSERT that follows.
_USAGE_BUDGET_S = 2.0


def _is_transient_db_error(exc: BaseException) -> bool:
    """Worth retrying: connection loss, pool/resource pressure, operator
    intervention, deadlock/serialization. A CHECK violation, an undefined
    column or bad data fails identically on every attempt, so retrying it
    only adds sleep to the user's time-to-completed."""
    return isinstance(exc, (
        TimeoutError,
        OSError,  # includes ConnectionError
        asyncpg.exceptions.PostgresConnectionError,
        asyncpg.exceptions.InterfaceError,
        asyncpg.exceptions.InsufficientResourcesError,
        asyncpg.exceptions.OperatorInterventionError,
        asyncpg.exceptions.TransactionRollbackError,
    ))


async def _insert_answer_run_with_retry(
    pg_pool: Any,
    sql: str,
    *args: Any,
) -> Any:
    """Run the silver.answer_runs INSERT, retrying transient failures.

    Three attempts, 0.25 s then 0.5 s apart (was 0.5/1.0/2.0 — 3.5 s of
    sleep on the critical path before `completed`, AGT-6). Only transient
    errors (``_is_transient_db_error``) are retried; a deterministic one
    re-raises at once. The caller bounds the whole thing with
    ``_PERSIST_BUDGET_S``.
    """
    last_exc: BaseException | None = None
    delays = (0.25, 0.5, 0.0)
    for attempt, delay in enumerate(delays, start=1):
        try:
            async with pg_pool.acquire() as conn:
                return await conn.fetchrow(sql, *args)
        except Exception as exc:  # noqa: BLE001 — classified below
            last_exc = exc
            if not _is_transient_db_error(exc):
                logger.warning(
                    "agentic_retrieval.persist: answer_runs INSERT failed "
                    "with a non-transient %s — not retrying",
                    type(exc).__name__,
                )
                raise
            if attempt < len(delays):
                logger.warning(
                    "agentic_retrieval.persist: answer_runs INSERT "
                    "attempt %d/%d failed (%s) — retrying in %.2fs",
                    attempt,
                    len(delays),
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "agentic_retrieval.persist: answer_runs INSERT "
                    "attempt %d/%d failed (%s) — retries exhausted",
                    attempt,
                    len(delays),
                    type(exc).__name__,
                )
    assert last_exc is not None  # noqa: S101 — invariant: loop ran at least once
    raise last_exc


async def persist_node(state: AgenticRetrievalState) -> dict[str, Any]:
    """Write the answer-run row + lineage payload.

    The legacy ``run_deterministic_rag`` has its own (much larger) answer-run
    persistence path. When the agentic flag is on, ``run_deterministic_rag``
    returns early and that path is skipped — so the agentic graph must
    persist independently or Phase 1.5 lineage stays dark.

    This node does the minimum required:

      1. Build a ``LineagePayload`` from the response + tool results
      2. Insert a single row into ``silver.answer_runs`` carrying the OIUR
         schema version, lineage JSONB columns, and basic model metadata.
         The INSERT is wrapped in
         :func:`_insert_answer_run_with_retry` — 3 attempts, 0.25 s / 0.5 s
         apart, transient errors only — and the FK check + INSERT together
         are bounded by ``_PERSIST_BUDGET_S`` (audit AGT-6: this node is on
         the path to the `completed` frame). Child rows and usage metering
         have their own smaller budgets; the trace is enqueued whatever
         happened to the INSERT.
      3. On terminal failure (all 3 retries exhausted) the answer has
         already been streamed back to the caller, so the answer_runs
         write is non-fatal — but we escalate: ``logger.error`` with
         ``extra={"alert": True}`` for Loki/Alertmanager, AND increment
         :data:`metrics.AGENTIC_PERSIST_FAILURES` so Prometheus can page
         on a sustained > 0 rate. Plan Step 1.5's fail-closed contract
         still applies to the legacy path (which retains the original
         strict-fail behaviour).

    The pg_pool comes from ``state.deps`` (whatever the FastAPI lifespan
    handed in); missing pool → no-op + log.
    """
    if state.response is None:
        return {}

    pg_pool = getattr(state.deps, "pg_pool", None)
    if pg_pool is None:
        logger.warning(
            "agentic_retrieval.persist: deps.pg_pool is None — skipping lineage write"
        )
        return {}

    project_id = getattr(state.deps, "project_id", None)
    from app.agent.workspace_context import WorkspaceContext  # noqa: PLC0415
    workspace_id = WorkspaceContext.from_state(
        state.deps, site="agentic_retrieval.persist_node",
    ).workspace_id

    try:
        from app.agent.lineage import build_lineage_payload  # noqa: PLC0415
        lineage = build_lineage_payload(
            response=state.response,
            fused_candidates=(),  # the agentic execute_node doesn't surface a fused list
        )
        cols = lineage.to_db_columns()
    except Exception:
        logger.exception("agentic_retrieval.persist: build_lineage_payload failed")
        return {}

    import json as _json

    from app.agent.guards import _drop_sentinel_citations  # noqa: PLC0415
    from app.config import settings as _settings  # noqa: PLC0415
    from app.models.answer_run import (  # noqa: PLC0415
        normalize_backend as _normalize_backend,
    )

    # `rejected` means no *real* citation survived. The assembler always
    # appends a `no-tool-call` placeholder so GeoRAGResponse's min_length=1
    # holds, which used to make this branch unreachable in production and
    # left every citation-less run persisted as `committed`. Same sentinel
    # filter the guard classifier uses (2026-09-07).
    citation_state = (
        "rejected" if not _drop_sentinel_citations(state.response.citations) else "committed"
    )

    # answer_runs.query_class has a CHECK constraint pinned to the spec
    # query-class literal (factual/spatial/document/computation/viz/unknown).
    # Map our intent labels onto that enum so the INSERT doesn't violate.
    _intent_to_spec_class: dict[str, str] = {
        "factual_lookup": "factual",
        "synthesis": "document",
        "hypothesis_generation": "document",
        "anomaly_detection": "computation",
        "uncertainty_quantification": "computation",
        "decision_support": "document",
        # ADR-0007 PR-1 — both new intents are SQL-aggregate-first, so they
        # map to the "computation" spec class (the CHECK constraint pinned
        # on answer_runs.query_class doesn't include 'aggregation').
        "project_summary": "computation",
        "coverage_gap": "computation",
    }
    spec_query_class = _intent_to_spec_class.get(
        state.effective_intent or state.intent or "", "unknown"
    )

    # RetrievalInspector follow-up — populate confidence + latency_ms +
    # capture the generated answer_run_id so the SSE `completed` frame can
    # surface it for the /retrieval/{id} deep link.
    _response_confidence: float | None = None
    _rc = getattr(state.response, "confidence", None)
    if isinstance(_rc, (int, float)):
        _response_confidence = float(_rc)

    _latency_ms: int | None = None
    if state.run_start_monotonic is not None:
        import time as _time_for_latency  # noqa: PLC0415
        _latency_ms = int(
            (_time_for_latency.monotonic() - state.run_start_monotonic) * 1000
        )

    # Run totals folded across every LLM-capable node — see
    # `_fold_token_usage` for why these ride on the state instead of being
    # read from the contextvar here.
    _input_tokens = int(getattr(state, "llm_input_tokens", 0) or 0)
    _output_tokens = int(getattr(state, "llm_output_tokens", 0) or 0)
    _answering_model = (
        getattr(state.response, "llm_model", None) or _settings.effective_llm_model
    )
    _backend_label = _normalize_backend(getattr(_settings, "LLM_BACKEND", None))
    _answer_run_id: str | None = None

    # §04i guard outcomes + refusal reason (2026-09-07 — see the helpers
    # above). Computed once here; the trace below reuses the codes.
    _guard_codes: list[Any] = _classify_persist_guards(state, citation_state)
    _guard_failure_codes: list[str] = [c.value for c in _guard_codes]
    _guard_results_json = _json.dumps(_build_guard_results(_guard_failure_codes))
    _rejection_reason = _build_rejection_reason(
        state, citation_state, _guard_failure_codes,
    )

    # Audit AGT-17: what the user SEES (guard codes for the error renderer,
    # the evidence packet for the per-kind cards, the "Interpreted as" chip)
    # is stamped before any database work. It used to be stamped inside the
    # trace block, which sat inside the INSERT's try — an INSERT exception
    # skipped it, and the UI lost the cards and the chip.
    _stamp_response_for_ui(state, _guard_failure_codes)

    row: Any = None
    _retr_count = 0
    _cite_count = 0
    try:
        # Audit AGT-6: bounded. Every step below waits on the pool; with no
        # bound, pool pressure or a slow RDS kept a fully streamed answer
        # from reaching `completed` until the 180 s deadline turned it into
        # a TIMEOUT frame.
        async with asyncio.timeout(_PERSIST_BUDGET_S):
            project_id = await _fk_checked_project_id(pg_pool, project_id)
            row = await _insert_answer_run_with_retry(
                pg_pool,
                _ANSWER_RUN_INSERT_SQL,
                workspace_id,
                project_id,
                # What the user asked, not the multi-turn rewrite (AGT-1);
                # the rewrite is in the trace's multi_turn_resolution.
                state.query_original or state.query,
                spec_query_class,
                citation_state,
                # The model that actually answered, not the one configured.
                # `llm_calls.record_run_llm_model()` stamps this inside the
                # same node as the LLM call and it rides here on the
                # response object; `settings.effective_llm_model` is only a
                # fallback for runs that produced no answer-bearing call.
                _answering_model,
                # Audit 2026-08-14 (finding 5): record the active LLM
                # backend, normalised onto the answer_runs_backend_valid
                # CHECK list so an unrecognised value persists as 'unknown'.
                _backend_label,
                cols["session_id"],
                _json.dumps(cols["lineage_retrieved_sources"]),
                _json.dumps(cols["lineage_filters_applied"]),
                _json.dumps(cols["lineage_qaqc_filters_applied"]),
                cols["answer_schema_version"],
                _response_confidence,
                _latency_ms,
                _input_tokens,
                _output_tokens,
                _rejection_reason,
                _guard_results_json,
                _reranker_version_for_run(state),
            )
    except TimeoutError:
        _report_persist_failure(
            "answer_runs INSERT failed after retries: persist budget of "
            f"{_PERSIST_BUDGET_S}s exceeded (pool wait or slow database)",
            exc_info=False,
        )
    except Exception:
        _report_persist_failure("answer_runs INSERT failed after retries")

    if row is not None:
        _answer_run_id = str(row["answer_run_id"])
        from uuid import UUID as _UUID  # noqa: PLC0415
        try:
            state.response.answer_run_id = _UUID(_answer_run_id)
        except Exception:
            # Pydantic assignment must never break observability.
            logger.debug(
                "agentic_retrieval.persist: failed to stamp "
                "answer_run_id on response",
                exc_info=True,
            )

        # RetrievalInspector follow-up — also persist the retrieval +
        # citation children so the inspector's Retrieval / Context panels
        # have data to render. Best-effort and separately bounded: the
        # parent row already landed.
        try:
            async with asyncio.timeout(_CHILD_ROWS_BUDGET_S):
                _retr_count, _cite_count = await _persist_retrieval_and_citation_items(
                    pg_pool=pg_pool,
                    answer_run_id=_answer_run_id,
                    workspace_id=workspace_id,
                    state=state,
                )
        except Exception:
            logger.exception(
                "agentic_retrieval.persist: child-row INSERTs failed or "
                "exceeded their budget (non-fatal)"
            )

        logger.info(
            "agentic_retrieval.persist: wrote answer_runs row "
            "(intent=%s schema_version=%s retrieved_sources=%d "
            "confidence=%s latency_ms=%s answer_run_id=%s "
            "retrieval_items=%d citation_items=%d)",
            state.effective_intent or state.intent,
            cols["answer_schema_version"],
            len(cols.get("lineage_retrieved_sources") or []),
            _response_confidence,
            _latency_ms,
            _answer_run_id,
            _retr_count,
            _cite_count,
        )

    # Plan §0e retrieval-trace observability. Audit AGT-17: this runs
    # whatever happened to the INSERT — it used to sit inside the INSERT's
    # try, so the "must run regardless" comment only covered `row is None`,
    # not an exception. `answer_run_id` is None when no row landed; the
    # RetrievalTrace schema allows it. enqueue_trace is a non-blocking
    # buffer append that never raises.
    await _enqueue_persist_trace(
        state,
        pg_pool=pg_pool,
        workspace_id=workspace_id,
        project_id=project_id,
        answer_run_id=row["answer_run_id"] if row is not None else None,
        guard_codes=_guard_codes,
        guard_failure_codes=_guard_failure_codes,
        citation_state=citation_state,
        latency_ms=_latency_ms,
    )

    # L1546 — meter the spend. Deliberately independent of the INSERT: the
    # tokens were bought whether or not the lineage row landed.
    # `_answer_run_id` is None when the INSERT never returned one;
    # usage.usage_events.invocation_id is nullable.
    try:
        async with asyncio.timeout(_USAGE_BUDGET_S):
            await _write_chat_usage_event(
                pg_pool,
                workspace_id=workspace_id,
                model_id=_answering_model,
                backend=_backend_label,
                input_tokens=_input_tokens,
                output_tokens=_output_tokens,
                latency_ms=_latency_ms,
                trace_id=getattr(state.deps, "trace_id", None),
                answer_run_id=_answer_run_id,
            )
    except TimeoutError:
        logger.error(
            "agentic_retrieval.persist: usage.usage_events INSERT exceeded its "
            "%.1fs budget — this query's spend is unmetered",
            _USAGE_BUDGET_S,
            extra={"alert": True, "workspace_id": workspace_id},
        )

    # Return the (possibly mutated) response so LangGraph propagates the
    # stamped answer_run_id back to the caller.
    return {"response": state.response}


_ANSWER_RUN_INSERT_SQL = """
    INSERT INTO silver.answer_runs (
        workspace_id,
        project_id,
        query_text,
        query_class,
        workspace_data_version_at_query,
        citation_lifecycle_state,
        model_name,
        backend_used,
        session_id,
        lineage_retrieved_sources,
        lineage_filters_applied,
        lineage_qaqc_filters_applied,
        answer_schema_version,
        confidence,
        latency_ms,
        input_tokens,
        output_tokens,
        rejection_reason,
        hallucination_guard_results,
        reranker_version,
        citation_mode
    ) VALUES (
        $1::uuid, $2::uuid, $3, $4, 0, $5, $6, $7, $8::uuid,
        $9::jsonb, $10::jsonb, $11::jsonb, $12, $13, $14, $15, $16,
        $17, $18::jsonb, $19,
        -- Audit RAG-22: never written before, so always NULL. CLAUDE.md
        -- rule 4: citation_mode is always posthoc_span_resolution.
        'posthoc_span_resolution'
    )
    RETURNING answer_run_id
"""

#: Child retrieval/citation rows — separately bounded (AGT-6).
_CHILD_ROWS_BUDGET_S = 2.0


def _report_persist_failure(message: str, *, exc_info: bool = True) -> None:
    """Terminal answer_runs failure: page, count, keep the answer.

    The answer has already streamed, so this path stays non-fatal — but the
    lineage row is permanently lost, so it logs at ERROR with
    ``extra={"alert": True}`` (Alertmanager) and increments
    AGENTIC_PERSIST_FAILURES (Prometheus rate alert).
    """
    try:
        from app.metrics import AGENTIC_PERSIST_FAILURES  # noqa: PLC0415

        AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs").inc()
    except Exception:  # pragma: no cover — never block on metrics
        logger.debug(
            "agentic_retrieval.persist: AGENTIC_PERSIST_FAILURES counter inc failed",
            exc_info=True,
        )
    logger.error(
        "agentic_retrieval.persist: %s", message,
        exc_info=exc_info, extra={"alert": True},
    )


async def _fk_checked_project_id(pg_pool: Any, project_id: Any) -> Any:
    """FK-safety (option (b) from §39 follow-up).

    silver.answer_runs.project_id has a FK to silver.projects. Callers
    occasionally pass workspace UUIDs or stale project_ids that don't
    resolve; the resulting ForeignKeyViolationError used to take down the
    whole persist. Validate-then-NULL keeps the row alive at the cost of one
    cheap SELECT. The FK column is nullable (ON DELETE SET NULL).
    """
    if project_id is None:
        return None
    try:
        async with pg_pool.acquire() as fk_conn:
            exists = await fk_conn.fetchval(
                "SELECT 1 FROM silver.projects WHERE project_id = $1::uuid",
                project_id,
            )
    except TimeoutError:
        raise
    except Exception:
        # Don't let the FK pre-check fail the persist: fall through with
        # the original project_id and let the INSERT decide.
        logger.debug(
            "agentic_retrieval.persist: project_id FK pre-check failed; "
            "trusting caller-supplied value",
            exc_info=True,
        )
        return project_id
    if exists is None:
        logger.warning(
            "agentic_retrieval.persist: project_id %s not present in "
            "silver.projects — dropping to NULL on INSERT",
            project_id,
        )
        return None
    return project_id


def _stamp_response_for_ui(
    state: AgenticRetrievalState, guard_failure_codes: list[str],
) -> None:
    """Fields the Laravel bridge / Chat.tsx render, stamped on the response.

    Each write is independent and best-effort; none may fail the answer.
    """
    response = state.response
    if response is None:
        return
    # Plan §4b — typed guard codes for the user-facing error renderer.
    try:
        response.guard_error_codes = list(guard_failure_codes)
    except Exception:  # pragma: no cover — defensive
        logger.debug(
            "agentic_retrieval.persist: failed to stamp guard_error_codes",
            exc_info=True,
        )
    # Plan §3a/§3b — the typed evidence packet, in `.model_dump()` form so
    # the SSE bridge can serialise it straight through; Chat.tsx dispatches
    # per-kind cards off `evidence_packet.evidence[].kind`.
    if state.evidence_packet is not None:
        try:
            response.evidence_packet = state.evidence_packet.model_dump(mode="json")
        except Exception:  # pragma: no cover — defensive
            logger.debug(
                "agentic_retrieval.persist: failed to stamp evidence_packet",
                exc_info=True,
            )
    # Plan §3e — the multi-turn resolution audit, for the "Interpreted as"
    # chip.
    if state.query_original is not None and state.resolution_trace:
        try:
            response.multi_turn_resolution = _multi_turn_resolution_payload(state)
        except Exception:  # pragma: no cover — defensive
            logger.debug(
                "agentic_retrieval.persist: failed to stamp multi_turn_resolution",
                exc_info=True,
            )


def _multi_turn_resolution_payload(state: AgenticRetrievalState) -> dict[str, Any]:
    return {
        "original_query": state.query_original,
        "rewritten_query": state.query,
        "trace": list(state.resolution_trace),
        "overall_confidence": state.resolution_confidence,
    }


def _result_row_count(result: Any) -> int:
    """Rows/chunks a tool result carries (AGT-17).

    The trace's per-source counts used to test isinstance(payload, list);
    every tool returns a dataclass, so raw_results_per_source was always 0
    and candidate_count_pre_rerank always None.
    """
    for attr in ("chunks", "records"):
        items = getattr(result, attr, None)
        if items is not None:
            return len(items)
    count = getattr(result, "count", None)
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    if isinstance(result, (list, tuple)):
        return len(result)
    return 0


_POSTGIS_TOOLS = frozenset({
    "query_spatial_collars", "query_assay_data", "query_downhole_logs",
    "query_collar_details", "query_project_overview",
})


async def _enqueue_persist_trace(
    state: AgenticRetrievalState,
    *,
    pg_pool: Any,
    workspace_id: Any,
    project_id: Any,
    answer_run_id: Any,
    guard_codes: list[Any],
    guard_failure_codes: list[str],
    citation_state: str,
    latency_ms: int | None,
) -> None:
    """Enqueue the silver.query_traces row. Never raises."""
    try:
        from app.agent.guards import GuardErrorCode  # noqa: PLC0415
        from app.services.trace_writer import (  # noqa: PLC0415
            GuardResults,
            LatencyBreakdown,
            RawResultsPerSource,
            RetrievalTrace,
            enqueue_trace,
        )

        source_counts: dict[str, int] = {
            "qdrant_dense": 0,
            "qdrant_sparse": 0,
            "postgis": 0,
            "neo4j": 0,
        }
        for tool_name, payload in state.tool_results:
            n = _result_row_count(payload)
            if tool_name in ("search_documents", "search_documents_adversarial"):
                # Hybrid retrieval returns one fused list; the dense/sparse
                # split is not recoverable here, so it is all booked dense.
                source_counts["qdrant_dense"] += n
            elif tool_name in _POSTGIS_TOOLS:
                source_counts["postgis"] += n
        candidate_total = sum(source_counts.values())

        # Plan §3a/§3b — prefer the typed EvidencePacket's `kind` list when
        # it's available (authority-ranked); fall back to tool names.
        if state.evidence_packet is not None and state.evidence_packet.evidence:
            evidence_types = [e.kind for e in state.evidence_packet.evidence]
        else:
            evidence_types = [name for name, _ in state.tool_results if name]

        selected_groups = (
            len(state.response.citations) if state.response is not None else 0
        )
        # Plan §3f — the packet's remaining_budget already has the system
        # prompt and evidence subtracted.
        remaining_budget = (
            state.evidence_packet.remaining_budget
            if state.evidence_packet is not None else None
        )

        generated_filters: dict[str, Any] = {}
        if state.retrieval_filters is not None:
            try:
                generated_filters = (
                    state.retrieval_filters.model_dump(exclude_none=True)
                    if hasattr(state.retrieval_filters, "model_dump")
                    else dict(state.retrieval_filters.__dict__)
                )
            except Exception:  # pragma: no cover — defensive
                logger.debug(
                    "agentic_retrieval.persist: retrieval_filters dump failed",
                    exc_info=True,
                )

        trace = RetrievalTrace(
            workspace_id=workspace_id,
            project_id=project_id,
            answer_run_id=answer_run_id,
            otel_trace_id=None,
            user_query=state.query_original or state.query,
            system_prompt_tokens=state.system_prompt_tokens_estimate,
            remaining_context_budget=remaining_budget,
            router_decision=str(state.intent) if state.intent else None,
            router_confidence=(
                float(state.intent_result.confidence)
                if state.intent_result is not None else None
            ),
            effective_intent=(
                str(state.effective_intent) if state.effective_intent else None
            ),
            tool_plan=(
                ", ".join(state.retrieval_profile.primary_tools)
                if state.retrieval_profile else None
            ),
            tool_calls=[
                {"name": name, "result_kind": type(payload).__name__}
                for name, payload in state.tool_results
            ],
            generated_filters=generated_filters,
            raw_results_per_source=RawResultsPerSource(**source_counts),
            candidate_count_pre_rerank=candidate_total or None,
            selected_context_groups=selected_groups or None,
            evidence_types_in_context=evidence_types,
            guard_results=GuardResults(
                numeric_grounding=GuardErrorCode.NUMERIC_GROUNDING_FAILED not in guard_codes,
                entity_grounding=GuardErrorCode.ENTITY_NOT_FOUND not in guard_codes,
                citation_completeness=GuardErrorCode.CITATION_INCOMPLETE not in guard_codes,
                refusal_triggered=citation_state == "rejected",
            ),
            guard_failure_codes=guard_failure_codes,
            # Plan §4b/§4c — what the repair planner would have attempted
            # (shadow) or did attempt (full).
            repair_strategies_used=list(state.repair_strategy_history),
            repair_attempts=len(state.repair_attempts),
            death_loop_triggered=state.repair_terminal_reason == "death loop detected",
            cache_hit=False,
            cache_type=None,
            latency_ms=LatencyBreakdown(total=latency_ms),
            context_prep_audit=state.context_prep_audit_payload,
            multi_turn_resolution=(
                _multi_turn_resolution_payload(state)
                if state.query_original is not None and state.resolution_trace
                else None
            ),
        )
        await enqueue_trace(pg_pool, trace)
    except Exception:
        logger.warning(
            "agentic_retrieval.persist: trace enqueue failed (non-fatal)",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Retrieval + citation children persistence — RetrievalInspector follow-up
# ---------------------------------------------------------------------------


def _maybe_uuid(s: Any) -> str | None:
    """Return ``str(uuid)`` when ``s`` parses cleanly as a UUID, else None.

    Used by the retrieval-items writer to decide whether a tool result's
    chunk_id can populate ``passage_id`` (real FK to silver.document_passages)
    or must be carried as opaque text inside ``candidate_ref`` JSONB instead.
    """
    if s is None:
        return None
    from uuid import UUID as _UUID  # noqa: PLC0415
    try:
        return str(_UUID(str(s)))
    except (ValueError, TypeError, AttributeError):
        return None


def _normalise_marker(citation_id: str | None) -> str | None:
    """Coerce a Citation marker into the silver.answer_citation_items CHECK shape.

    The DB CHECK constraint is ``^\\[(DATA|NI43|PUB|PGEO|ev):[A-Za-z0-9-]+\\]$``
    — colon-separated, with one of the four document-type prefixes or the
    ``ev:`` evidence-id form. The Citation model on GeoRAGResponse
    historically used hyphen-separated markers (``[DATA-1]``); normalise
    both shapes onto the canonical colon form so the INSERT passes the
    CHECK constraint. Anything else (typo prefix, missing brackets,
    spaces) returns None so the citation gets dropped rather than write
    a row the DB would reject.
    """
    if not citation_id:
        return None
    import re as _re  # noqa: PLC0415

    s = str(citation_id).strip()
    # Canonical colon form — must still match the prefix whitelist; the
    # DB CHECK would reject [BAD:1] otherwise.
    if _re.match(r"^\[(DATA|NI43|PUB|PGEO|ev):[A-Za-z0-9-]+\]$", s):
        return s
    # Legacy hyphen form: [DATA-1] / [NI43-2] / [PUB-3] / [PGEO-4].
    m = _re.match(r"^\[(DATA|NI43|PUB|PGEO|ev)-([A-Za-z0-9-]+)\]$", s)
    if m:
        return f"[{m.group(1)}:{m.group(2)}]"
    return None


def _citation_source_store(citation_type: str | None) -> str | None:
    """Map a Citation.citation_type onto the source_store CHECK enum."""
    if not citation_type:
        return None
    t = citation_type.upper()
    if t in ("DATA", "NI43", "PUB", "PGEO"):
        # All four citation types currently come from the Qdrant document
        # store via search_documents. neo4j / postgis citations would only
        # appear if a future tool returned graph or spatial provenance as
        # an inline citation.
        return "qdrant"
    return None


def _extract_retrieval_rows(
    tool_results: list[tuple[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten tool results into retrieval-item row payloads.

    Returns a list of dicts ready for the INSERT — each carries
    ``source_store``, optional ``passage_id`` (UUID str), ``candidate_ref``
    JSON-serialisable dict, and a ``retriever_score`` when the tool surfaces
    one. Currently handles two shapes:

      * ``DocumentSearchResult`` from ``search_documents`` → one row per
        ``chunks[i]`` with passage_id when chunk_id parses as UUID.
      * ``CollarDetailsResult`` from ``query_collar_details`` → a single
        ``postgis`` candidate_ref row (no passage_id; the collar is the
        retrieval target).

    Other tool result shapes are ignored for now — the Inspector page
    surfaces what we have and shows "No retrieval items recorded." when
    nothing maps cleanly. Future tools can extend the dispatcher here
    without touching the INSERT site.
    """
    rows: list[dict[str, Any]] = []
    for tool_name, result in tool_results:
        # Document chunks (Qdrant)
        #
        # `search_documents` runs the BGE cross-encoder reranker inline
        # (see TestSearchDocuments::test_reranker_overwrites_cosine_scores_*
        # — the reranker overwrites `relevance_score` with the post-rerank
        # logit and sorts in place). So a chunk reaching this point has
        # already been through the rerank stage; we mark it as such and
        # store the score on `reranker_score`. The Inspector's Rerank
        # panel filters on stage='reranked' so this lights it up.
        chunks = getattr(result, "chunks", None)
        if chunks is not None:
            for chunk in chunks:
                chunk_id = getattr(chunk, "chunk_id", None)
                passage_id = _maybe_uuid(chunk_id)
                candidate_ref = {
                    "store": "qdrant",
                    "tool": tool_name,
                    "chunk_id": str(chunk_id) if chunk_id is not None else None,
                    "document_title": getattr(chunk, "document_title", None),
                    "section": getattr(chunk, "section", None)
                                or getattr(chunk, "section_title", None),
                    "section_number": getattr(chunk, "section_number", None),
                    "page": getattr(chunk, "page", None),
                    "document_type": getattr(chunk, "document_type", None),
                    "snippet": (getattr(chunk, "text", "") or "")[:280],
                }
                rows.append({
                    "stage": "reranked",
                    "source_store": "qdrant",
                    "passage_id": passage_id,
                    "candidate_ref": candidate_ref,
                    "reranker_score": getattr(chunk, "relevance_score", None),
                    "retriever_score": None,
                })
            continue

        # Collar detail lookup (PostGIS) — no rerank stage applies to
        # direct PK lookups, so this stays 'retrieved'.
        if getattr(result, "collar_id", None) is not None:
            rows.append({
                "stage": "retrieved",
                "source_store": "postgis",
                "passage_id": None,
                "candidate_ref": {
                    "store": "postgis",
                    "tool": tool_name,
                    "table": "silver.collars",
                    "pk": {"collar_id": str(result.collar_id)},
                    "hole_id": getattr(result, "hole_id", None),
                    "document_title": (
                        f"Drill hole {getattr(result, 'hole_id', '')}".strip()
                        or "Drill hole"
                    ),
                    "snippet": _summarise_collar(result),
                },
                "retriever_score": 1.0,  # direct lookup → max
                "reranker_score": None,
            })
    return rows


def _summarise_collar(result: Any) -> str:
    """Build a short, human-readable snippet for a CollarDetailsResult."""
    parts: list[str] = []
    hole_id = getattr(result, "hole_id", None)
    if hole_id:
        parts.append(f"Hole {hole_id}")
    drill_type = getattr(result, "drill_type", None)
    if drill_type:
        parts.append(str(drill_type))
    depth = getattr(result, "total_depth", None)
    if depth is not None:
        parts.append(f"total depth {depth} m")
    drill_date = getattr(result, "drill_date", None)
    if drill_date:
        parts.append(f"drilled {drill_date}")
    return ", ".join(parts) or "drill collar"


def _extract_citation_rows(
    citations: list[Any],
    retrieval_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map GeoRAGResponse.citations onto answer_citation_items row payloads.

    Skips citations that can't satisfy the CHECK constraints:
      * marker_text must match the regex (DATA|NI43|PUB|PGEO|ev):X.
      * one of evidence_id / passage_id must be non-null. We resolve
        passage_id by matching the citation's ``source_chunk_id`` to one
        of the retrieval rows; if no match (e.g. citation backed by a
        non-passage tool result), the citation is dropped from the
        inspector view rather than silently writing a bogus row.

    De-duplicates on marker_text to honour the
    ``answer_citation_items_unique_per_run`` constraint.
    """
    # Build a chunk_id → passage_id lookup from the retrieval rows.
    chunk_to_passage: dict[str, str] = {}
    for r in retrieval_rows:
        cid = (r.get("candidate_ref") or {}).get("chunk_id")
        pid = r.get("passage_id")
        if cid and pid:
            cid = str(cid)
            # Symmetric `:chunk=` handling with the citation loop below.
            if ":chunk=" in cid:
                cid = cid.rsplit(":chunk=", 1)[-1]
            chunk_to_passage[cid] = pid

    rows: list[dict[str, Any]] = []
    seen_markers: set[str] = set()
    for c in citations or ():
        marker = _normalise_marker(getattr(c, "citation_id", None))
        if marker is None or marker in seen_markers:
            continue
        raw = str(getattr(c, "source_chunk_id", "") or "")
        # Document citations carry a composite source_chunk_id
        # ("georag_reports:<report_id>:section=..:chunk=<uuid>") — the
        # passage lookup wants the bare chunk uuid after `:chunk=`.
        chunk_id = raw.rsplit(":chunk=", 1)[-1] if ":chunk=" in raw else raw
        passage_id = chunk_to_passage.get(chunk_id) or _maybe_uuid(chunk_id)
        if passage_id is None:
            # No passage backing this citation — the CHECK constraint
            # rejects evidence_id=NULL + passage_id=NULL. Skip rather
            # than fabricate an evidence_id.
            continue
        seen_markers.add(marker)
        rows.append({
            "marker_text": marker,
            "passage_id": passage_id,
            "source_store": _citation_source_store(
                getattr(c, "citation_type", None)
            ),
            "confidence": getattr(c, "relevance_score", None),
        })
    return rows


async def _batched_executemany(
    conn: Any, sql: str, args: list[tuple[Any, ...]],
) -> int | None:
    """One executemany; the row count on success, None if the batch was
    rejected (asyncpg's executemany is atomic, so nothing landed and the
    caller's per-row path can run without duplicating anything)."""
    if not args:
        return 0
    try:
        await conn.executemany(sql, args)
    except Exception:
        logger.debug(
            "agentic_retrieval.persist: batched child-row INSERT rejected — "
            "falling back to per-row inserts",
            exc_info=True,
        )
        return None
    return len(args)


async def _batched_retrieval_insert(
    conn: Any,
    retr_rows: list[dict[str, Any]],
    cited_chunk_ids: set[str],
    answer_run_id: str,
    workspace_id: str,
    sql_with_passage: str,
    sql_null_passage: str,
) -> int | None:
    """Both retrieval-item batches in one transaction, or neither."""
    import json as _json  # noqa: PLC0415

    with_passage: list[tuple[Any, ...]] = []
    null_passage: list[tuple[Any, ...]] = []
    for r in retr_rows:
        ref = r.get("candidate_ref") or {}
        used = str(ref.get("chunk_id") or "") in cited_chunk_ids
        stage = r.get("stage") or "retrieved"
        tail = (_json.dumps(ref), r.get("retriever_score"), r.get("reranker_score"), used)
        if r.get("passage_id") is not None:
            with_passage.append(
                (answer_run_id, workspace_id, stage, r["source_store"], r["passage_id"], *tail)
            )
        else:
            null_passage.append(
                (answer_run_id, workspace_id, stage, r["source_store"], *tail)
            )
    try:
        async with conn.transaction():
            if with_passage:
                await conn.executemany(sql_with_passage, with_passage)
            if null_passage:
                await conn.executemany(sql_null_passage, null_passage)
    except Exception:
        logger.debug(
            "agentic_retrieval.persist: batched retrieval_item INSERT rejected "
            "(typically a passage not in silver.document_passages) — falling "
            "back to per-row inserts",
            exc_info=True,
        )
        return None
    return len(with_passage) + len(null_passage)


async def _persist_retrieval_and_citation_items(
    *,
    pg_pool: Any,
    answer_run_id: str,
    workspace_id: str,
    state: AgenticRetrievalState,
) -> tuple[int, int]:
    """Batch-write retrieval + citation child rows. Returns (#retr, #cite)."""
    import json as _json  # noqa: PLC0415

    retr_rows = _extract_retrieval_rows(state.tool_results or [])
    cite_rows = _extract_citation_rows(
        list(getattr(state.response, "citations", None) or ()),
        retr_rows,
    )

    # Mark retrieval rows that are referenced by a citation so the
    # Inspector can highlight them. Lookup is on the candidate_ref's
    # chunk_id, which is the same key used in _extract_citation_rows.
    cited_chunk_ids: set[str] = set()
    for c in cite_rows:
        for r in retr_rows:
            ref = r.get("candidate_ref") or {}
            if r.get("passage_id") == c["passage_id"] and ref.get("chunk_id"):
                cited_chunk_ids.add(str(ref["chunk_id"]))

    retr_count = 0
    if retr_rows:
        # Two SQL variants: one binds passage_id, the other forces it
        # NULL. We try the FK form first, and on FK violation fall back
        # to the NULL form so an item whose chunk hasn't been ingested
        # yet (or comes from a non-passage tool) still appears in the
        # inspector via candidate_ref. The "ForeignKeyViolationError"
        # path is narrowly typed so we don't swallow genuine bugs.
        #
        # The `stage` column is supplied by the caller (search_documents
        # results arrive post-rerank → 'reranked'; direct PK lookups →
        # 'retrieved') so the Inspector's Rerank panel can filter on it.
        retr_sql_with_passage = """
            INSERT INTO silver.answer_retrieval_items (
                answer_run_id, workspace_id, stage, source_store,
                passage_id, candidate_ref, retriever_score, reranker_score,
                included_in_context, used_in_citation
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5::uuid, $6::jsonb, $7, $8, TRUE, $9
            )
        """
        retr_sql_null_passage = """
            INSERT INTO silver.answer_retrieval_items (
                answer_run_id, workspace_id, stage, source_store,
                passage_id, candidate_ref, retriever_score, reranker_score,
                included_in_context, used_in_citation
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4,
                NULL, $5::jsonb, $6, $7, TRUE, $8
            )
        """
        async with pg_pool.acquire() as conn:
            # AGT-6: one round trip for the whole batch when every row is
            # clean; the per-row loop below (with its FK -> NULL-passage
            # fallback) runs only if the batch was rejected.
            batched = await _batched_retrieval_insert(
                conn, retr_rows, cited_chunk_ids, answer_run_id, workspace_id,
                retr_sql_with_passage, retr_sql_null_passage,
            )
            retr_count = batched or 0
            for r in (retr_rows if batched is None else ()):
                ref_dict = r.get("candidate_ref") or {}
                used_in_citation = (
                    str(ref_dict.get("chunk_id") or "") in cited_chunk_ids
                )
                passage_id = r.get("passage_id")
                stage = r.get("stage") or "retrieved"
                inserted = False
                if passage_id is not None:
                    try:
                        await conn.execute(
                            retr_sql_with_passage,
                            answer_run_id,
                            workspace_id,
                            stage,
                            r["source_store"],
                            passage_id,
                            _json.dumps(ref_dict),
                            r.get("retriever_score"),
                            r.get("reranker_score"),
                            used_in_citation,
                        )
                        inserted = True
                    except asyncpg.exceptions.ForeignKeyViolationError:
                        logger.debug(
                            "agentic_retrieval.persist: passage_id=%s not in "
                            "silver.document_passages — retrying with NULL",
                            passage_id,
                        )
                    except Exception:
                        logger.debug(
                            "agentic_retrieval.persist: retrieval_item INSERT "
                            "skipped (non-fatal)",
                            exc_info=True,
                        )
                if not inserted:
                    try:
                        await conn.execute(
                            retr_sql_null_passage,
                            answer_run_id,
                            workspace_id,
                            stage,
                            r["source_store"],
                            _json.dumps(ref_dict),
                            r.get("retriever_score"),
                            r.get("reranker_score"),
                            used_in_citation,
                        )
                        inserted = True
                    except Exception:
                        logger.debug(
                            "agentic_retrieval.persist: retrieval_item "
                            "NULL-passage INSERT skipped (non-fatal)",
                            exc_info=True,
                        )
                if inserted:
                    retr_count += 1

    cite_count = 0
    if cite_rows:
        cite_sql = """
            INSERT INTO silver.answer_citation_items (
                answer_run_id, workspace_id, passage_id,
                marker_text, source_store, confidence
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5, $6
            )
            ON CONFLICT (answer_run_id, marker_text) DO NOTHING
        """
        async with pg_pool.acquire() as conn:
            cite_args = [
                (
                    answer_run_id, workspace_id, c["passage_id"], c["marker_text"],
                    c.get("source_store"), c.get("confidence"),
                )
                for c in cite_rows
            ]
            batched = await _batched_executemany(conn, cite_sql, cite_args)
            cite_count = batched or 0
            for c in (cite_rows if batched is None else ()):
                try:
                    await conn.execute(
                        cite_sql,
                        answer_run_id,
                        workspace_id,
                        c["passage_id"],
                        c["marker_text"],
                        c.get("source_store"),
                        c.get("confidence"),
                    )
                    cite_count += 1
                except asyncpg.exceptions.ForeignKeyViolationError:
                    # Citation references a passage that's not in
                    # silver.document_passages — most often because the
                    # chunk was demoted / purged. The CHECK constraint
                    # forbids NULL passage_id AND NULL evidence_id, so
                    # we drop the row entirely.
                    logger.debug(
                        "agentic_retrieval.persist: citation passage_id=%s "
                        "missing — dropping citation %s",
                        c["passage_id"],
                        c["marker_text"],
                    )
                except Exception:
                    logger.debug(
                        "agentic_retrieval.persist: citation_item INSERT "
                        "skipped (non-fatal)",
                        exc_info=True,
                    )

    return retr_count, cite_count


# ---------------------------------------------------------------------------
# Plan §2c — entity-resolver shadow pass (called from execute_node)
# ---------------------------------------------------------------------------


async def _entity_resolver_shadow(
    state: AgenticRetrievalState, hole_ids: list[str],
) -> None:
    """Plan §2c — resolve extracted hole IDs against silver.entity_aliases.

    No-op when ENTITY_RESOLVER_SHADOW_ENABLED is False (default) OR
    when the deps lack a pg_pool. Pure telemetry — never modifies
    state. Hits log canonical names; misses INSERT into silver.alias_gaps
    so the SME review queue catches them.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.ENTITY_RESOLVER_SHADOW_ENABLED:
        return

    pool = getattr(state.deps, "pg_pool", None)
    if pool is None:
        return

    workspace_id = getattr(state.deps, "workspace_id", None)
    if not workspace_id:
        return

    try:
        from app.agent.entity_resolver import resolve_entity  # noqa: PLC0415
    except Exception:  # pragma: no cover — defensive
        logger.exception("entity_resolver_shadow: import failed")
        return

    for hid in hole_ids:
        try:
            result = await resolve_entity(
                pool,
                workspace_id=workspace_id,
                entity_type="hole_id",
                entity_text=hid,
                gap_detector="hole_id_extractor",
            )
            logger.info(
                "agentic_retrieval.entity_resolver_shadow: hole_id=%r match_kind=%s confidence=%.2f canonical=%s",
                hid,
                result.match_kind,
                result.confidence,
                result.canonical_name,
            )
        except Exception:  # pragma: no cover — defensive
            logger.warning(
                "entity_resolver_shadow: lookup failed for %r (non-fatal)",
                hid,
                exc_info=True,
            )


__all__ = [
    "assemble_node",
    "classify_node",
    "demote_node",
    "execute_node",
    "persist_node",
    "repair_shadow_node",
    "resolve_node",
    "route_node",
    "validate_node",
]
