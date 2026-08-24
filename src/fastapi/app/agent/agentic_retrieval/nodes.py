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
import contextlib
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
        from app.agent.multi_turn_resolver import resolve_multi_turn  # noqa: PLC0415

        resolved = resolve_multi_turn(state.query, list(state.history))

        # Plan §I — multi_turn.* Sentry tags. Mutate state in place
        # with the resolved values BEFORE stamping so the stamper reads
        # the post-resolve view. State is a Pydantic v2 BaseModel (not
        # frozen) so attribute set is allowed. The LangGraph merge of
        # the returned update dict happens after this; the in-place
        # mutation is what the spec wants ("immediately after
        # resolve_multi_turn returns").
        try:
            state.resolution_trace = [
                {
                    "kind": s.kind,
                    "original_phrase": s.original_phrase,
                    "resolved_to": s.resolved_to,
                    "source_turn_index": s.source_turn_index,
                    "confidence": s.confidence,
                }
                for s in resolved.resolution_trace
            ]
            state.resolution_confidence = resolved.overall_confidence
            from app.agent.sentry_tags import stamp_multi_turn_tags  # noqa: PLC0415
            stamp_multi_turn_tags(state)
        except Exception:  # pragma: no cover — defensive
            logger.debug("multi_turn sentry stamp failed (non-fatal)", exc_info=True)

        if not resolved.made_changes:
            # Stamp the confidence even when no substitution happened —
            # a fully-resolvable query is a positive signal.
            return {"resolution_confidence": resolved.overall_confidence}

        logger.info(
            "agentic_retrieval.resolve: rewrote query "
            "(steps=%d, confidence=%.2f)",
            len(resolved.resolution_trace),
            resolved.overall_confidence,
        )

        return {
            "query": resolved.rewritten_query,
            "query_original": state.query,
            "resolution_trace": [
                {
                    "kind": s.kind,
                    "original_phrase": s.original_phrase,
                    "resolved_to": s.resolved_to,
                    "source_turn_index": s.source_turn_index,
                    "confidence": s.confidence,
                }
                for s in resolved.resolution_trace
            ],
            "resolution_confidence": resolved.overall_confidence,
        }
    except Exception:  # pragma: no cover — defensive
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
    result = await classify_intent(state.query, openai_http_client=openai_client)
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
        return extract_hole_ids(query)[:3]
    except Exception:  # pragma: no cover — defensive
        logger.exception("agentic_retrieval.execute: extract_hole_ids failed")
        return []


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
        if real_name in (
            "query_spatial_collars",
            "query_assay_data",
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

    primary_results = await asyncio.gather(
        *(_dispatch_primary(tool_name) for tool_name in profile.primary_tools)
    )
    for tool_name, result in primary_results:
        if _worth_citing(tool_name, result):
            results.append((tool_name, result))

    # Adversarial pass (hypothesis-generation only) — re-issue the
    # document-search tool against a disconfirming query framing.
    if profile.adversarial_pass_enabled and _allowed("search_documents_adversarial"):
        adversarial_query = _build_adversarial_query(state.query)
        result = await _call_tool_safely(
            "search_documents", adversarial_query, state.deps
        )
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
    # We invoke them only when the primary pass yielded fewer than a small
    # hardcoded number of results, a cheap heuristic for "we need more
    # coverage". NOTE (audit 2026-06-28): this threshold is a literal, NOT
    # profile.max_chunks — that field is declared but not yet wired (see
    # RetrievalProfile). Keep the comment honest so the config isn't assumed
    # to drive this branch.
    _SECONDARY_COVERAGE_THRESHOLD = 3
    if len(results) < _SECONDARY_COVERAGE_THRESHOLD and profile.secondary_tools:
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
        if result is not None:
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
            if result is not None:
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
        _query_id = str(_uuid4())

        raw_packet = build_evidence_packet(
            query_id=_query_id,
            query_text=state.query,
            tool_results=results,
            system_prompt_tokens=state.system_prompt_tokens_estimate or 0,
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

    return {"tool_results": results, "evidence_packet": evidence_packet}


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

    Truncation observability (2026-08-15): ``_TOTAL_BUDGET`` caps this
    block at 24K chars. Blocks render in ``tool_results`` dispatch order
    (NOT relevance order), so on document-heavy retrievals the budget can
    be exhausted before structured collar/assay/downhole data ever
    renders — silently, with only a bare ``"[context budget reached]"``
    marker in the prompt and nothing in the logs. When truncation occurs
    we now log a ``warning`` naming every dropped block's tool + citation
    id and approximate size, plus the optional ``query``/``workspace_id``
    context callers may supply, so a truncated-context incident can
    actually be found and measured later. This does NOT change what gets
    dropped or in what order — that prioritization problem is a separate,
    larger design question — it only makes the drop observable.
    """
    from app.agent.context_builder import _UNTRUSTED_GUARD, _fence_untrusted  # noqa: PLC0415
    from app.agent.response_assembler import assign_citation_ids  # noqa: PLC0415
    from app.config import settings as _settings  # noqa: PLC0415

    # Imported, not re-typed. Chunks are already bounded at ingest to
    # WINDOW_CHARS, and this used to cut them again at 1,800 — 64% of every
    # retrieved chunk was discarded before the model saw it, while the
    # citation for that chunk went into the answer regardless. A citation
    # pointing at text nobody read is worse than no citation: it looks
    # like evidence. pdf_report's own sizing comment says the window was
    # chosen so five chunks land at ~6,250 tokens; this cap was what stopped
    # that from happening.
    from app.services.ingest.pdf_report import WINDOW_CHARS  # noqa: PLC0415

    _CHUNK_TEXT_CAP = WINDOW_CHARS   # i.e. never cut a chunk mid-way
    _STRUCTURED_CAP = 1200           # per structured (non-chunk) result

    # Derived from the active backend's context window rather than frozen.
    # 24,000 characters was sized for the 16K-context local vLLM deployment
    # and never revisited after the move to Azure Foundry, where the window
    # is 100,000 TOKENS — so the evidence budget was about 6% of what was
    # available, and the shortfall was paid for by truncating chunks.
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
    _fence_enabled = bool(_settings.PROMPT_INJECTION_DELIMITING_ENABLED)

    # Each entry is (rendered_text, tool_name, citation_id) — the tool
    # name + citation id are carried alongside the text purely so a
    # budget-truncation event can name what got dropped (see docstring).
    blocks: list[tuple[str, str, str]] = []
    id_bundles = assign_citation_ids(tool_results)
    for (tool_name, result), bundle in zip(
        tool_results, id_bundles, strict=False
    ):
        chunks = getattr(result, "chunks", None)
        records = getattr(result, "records", None)
        if chunks is not None:
            # DocumentSearchResult-shaped — one block per retrieved chunk,
            # each carrying its OWN citation id (audit 2026-08-14 finding 1:
            # assign_citation_ids now emits one id per chunk, mirroring the
            # PGEO per-record branch, so the marker the model cites maps to
            # the real chunk_id/section/page in the assembled Citation).
            # zip is deliberately non-strict: an empty result has one
            # sentinel id and zero chunks.
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
                page = getattr(chunk, "page", None)
                if page is not None:
                    header += f" | page {page}"
                # annotated_text, not text. A page-image chunk's content is
                # a vision model's DESCRIPTION of the page, and annotated_text
                # is what carries the "not quoted text from the document"
                # prefix. Reading .text here stripped that prefix, so a
                # generated description reached the model looking exactly
                # like an extract it could quote. context_builder.py:144 on
                # the legacy path already read annotated_text; this path did
                # not.
                text = (getattr(chunk, "annotated_text", None)
                        or getattr(chunk, "text", "")
                        or "")[:_CHUNK_TEXT_CAP]
                if _fence_enabled:
                    text = _fence_untrusted(text)
                blocks.append((f"{header}\n{text}", tool_name, cid))
        elif records is not None and len(bundle) == len(records):
            # PublicGeoscienceSearchResult-shaped — one id per record.
            for record, cid in zip(records, bundle, strict=False):
                record_text = str(record)[:_STRUCTURED_CAP]
                if _fence_enabled:
                    record_text = _fence_untrusted(record_text)
                blocks.append((f"{cid} tool={tool_name} {record_text}", tool_name, cid))
        else:
            # Structured results (collars / samples / graph / overview) —
            # one compact block per tool result. Sourced from our own
            # PostGIS/Neo4j tables, not externally-authored free text, so
            # (matching _build_context) these are NOT fenced.
            cid = bundle[0] if bundle else "[DATA-0]"
            blocks.append((
                f"{cid} tool={tool_name} {str(result)[:_STRUCTURED_CAP]}",
                tool_name,
                cid,
            ))

    out: list[str] = []
    total = 0
    dropped: list[tuple[str, str, int]] = []  # (tool_name, citation_id, size)
    if _fence_enabled and blocks:
        out.append(_UNTRUSTED_GUARD)
        out.append("")
        total += len(_UNTRUSTED_GUARD) + 1
    for i, (block, _tool_name, _cid) in enumerate(blocks):
        if total + len(block) > _TOTAL_BUDGET:
            out.append("[context budget reached]")
            dropped = [(tn, c, len(b)) for b, tn, c in blocks[i:]]
            break
        out.append(block)
        total += len(block)

    if dropped:
        _dropped_chars = sum(size for _, _, size in dropped)
        logger.warning(
            "agentic_retrieval.assemble: context budget reached "
            "(%d/%d chars used) — dropped %d/%d tool-result block(s) "
            "totalling ~%d chars that never reached the LLM context "
            "(dropped tool/citation ids: %s); query=%r workspace_id=%s",
            total,
            _TOTAL_BUDGET,
            len(dropped),
            len(blocks),
            _dropped_chars,
            [f"{tn}:{c}" for tn, c, _ in dropped[:25]],
            query,
            workspace_id,
        )

    return "\n\n".join(out) if out else "(no tool results)"


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
    from app.agent.llm_calls import _call_llm  # noqa: PLC0415
    from app.agent.orchestrator import _select_system_prompt  # noqa: PLC0415
    from app.agent.response_assembler import assemble_response  # noqa: PLC0415
    from app.config import settings as _settings  # noqa: PLC0415

    # Plan §3 — when CONTEXT_PREP_ENABLED is set, run the EvidencePacket
    # through the per-intent prepare_evidence_for_intent pipeline. The
    # prepared packet replaces state.evidence_packet AND drives the
    # context_block. When the flag is off (default) the legacy
    # tool_results → context_block path runs as before.
    #
    # Best-effort: any failure inside the prep pipeline logs but does
    # NOT block the answer path — we fall back to the legacy path so
    # the user still gets an answer (degraded, but not broken).
    prep_audit_for_state: Any = None
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
            prep_audit_for_state = prepared
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

    # Plan §I — context_prep.* Sentry tags. Stamped UNCONDITIONALLY
    # (not just when CONTEXT_PREP_ENABLED is on) so the dashboard's
    # `context_prep.enabled` filter always shows a value. The stamper
    # picks safe defaults when prep_audit_for_state is None.
    try:
        from app.agent.sentry_tags import stamp_context_prep_tags  # noqa: PLC0415
        stamp_context_prep_tags(state, prep_audit_for_state)
    except Exception:  # pragma: no cover — defensive
        logger.debug("context_prep sentry stamp failed (non-fatal)", exc_info=True)

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
        context_block = _render_tool_results_context(
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
        categories=_categories_from_tool_results(state.tool_results),
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
        query=state.query,
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
        state.tool_results,
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
    return {"response": response, **_fold_token_usage(state)}


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
                    "trace_points": c.trace_points,
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
    """Run the Layer-2/3/4/6 post-assembly validation the legacy path uses.

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
    orchestrator body. Wired in here, last, since it's a pure enrichment
    pass (never rejects/mutates the answer text, only appends source-file
    provenance onto Citation.section) and is cheapest to run once the
    response is otherwise final.

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
        validate_and_repair,
    )
    from app.agent.hallucination.layer5_provenance import (  # noqa: PLC0415
        enrich_provenance,
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

    # Layer 2 — typed-output repair (sync, never raises).
    response = validate_and_repair(state.response)

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
        return {"response": response, "validation_warnings": [_unverified_warning]}

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
        _reason = warnings[0] if warnings else "an unverified claim"
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

    Crucially, does NOT:

      - Modify ``state.response``, ``state.retrieval_profile``, or
        ``state.retrieval_filters``.
      - Re-issue any retrieval node.
      - Bump ``state.repair_attempts`` (no actual attempt happened —
        the full loop is what records attempts).

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

        # Plan §I — repair.* Sentry tags. Stamped after any loop driver
        # has run so `repair.attempts` reflects the final count. Read
        # from state directly (the loop driver mutates state in place
        # before returning its update dict, so the counts are live).
        try:
            from app.agent.sentry_tags import stamp_repair_tags  # noqa: PLC0415
            # Mutate state with the freshly-derived codes so the stamper
            # sees them — LangGraph hasn't merged update_dict yet.
            state.repair_codes_observed = codes_str
            stamp_repair_tags(state, plan)
        except Exception:  # pragma: no cover — defensive
            logger.debug("repair sentry stamp failed (non-fatal)", exc_info=True)

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
        "response": state.response,
        "tool_results": list(state.tool_results),
        "retrieval_filters": state.retrieval_filters,
        "retrieval_profile": state.retrieval_profile,
        "repair_attempts": attempts,
        "repair_strategy_history": history_str,
        "repair_terminal_reason": (
            "death loop detected" if death_loop_triggered
            else "loop completed"
        ),
    }


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
    context_block = _render_tool_results_context(
        state.tool_results,
        query=state.query,
        workspace_id=getattr(state.deps, "workspace_id", None),
    )

    # Same variant the first attempt used — a repair pass that switched
    # prompt variants mid-loop would be repairing against different rules
    # than the answer it is repairing.
    system_prompt = _select_system_prompt(
        categories=_categories_from_tool_results(state.tool_results),
        query=state.query,
    ) + suffix

    openai_client = getattr(state.deps, "openai_http_client", None)
    anthropic_client = getattr(state.deps, "anthropic_client", None)

    text = await _call_llm(
        query=state.query,
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

    new_response = assemble_response(text, state.tool_results)
    # Preserve the answer_run_id stamped by persist_node on the prior
    # attempt (the row already exists; we're re-rendering text only).
    if state.response is not None:
        with contextlib.suppress(Exception):
            new_response.answer_run_id = state.response.answer_run_id
    state.response = new_response


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

    # Re-run execute then assemble.
    exec_update = await execute_node(state)
    if "tool_results" in exec_update:
        state.tool_results = exec_update["tool_results"]
    if "evidence_packet" in exec_update:
        state.evidence_packet = exec_update["evidence_packet"]

    asm_update = await assemble_node(state)
    if "response" in asm_update:
        state.response = asm_update["response"]


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
        "message": (
            f"Terminal repair strategy triggered: {strategy.value}. "
            f"See the user-facing surface for next steps."
        ),
        "candidates": [],
        "guard_codes": code_values,
    }


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


async def _insert_answer_run_with_retry(
    pg_pool: Any,
    sql: str,
    *args: Any,
) -> Any:
    """Run the silver.answer_runs INSERT with bounded exponential backoff.

    Three attempts spaced 0.5s → 1.0s → 2.0s. Transient asyncpg /
    PostgreSQL errors during answer write-out (PgBouncer saturation, brief
    network blip, PG restart) shouldn't cost us a lineage row. The final
    failure re-raises so the caller can decide whether to escalate.
    """
    import asyncio as _asyncio  # noqa: PLC0415

    last_exc: BaseException | None = None
    delays = (0.5, 1.0, 2.0)
    for attempt, delay in enumerate(delays, start=1):
        try:
            async with pg_pool.acquire() as conn:
                return await conn.fetchrow(sql, *args)
        except Exception as exc:  # noqa: BLE001 — bounded retry surface
            last_exc = exc
            if attempt < len(delays):
                logger.warning(
                    "agentic_retrieval.persist: answer_runs INSERT "
                    "attempt %d/%d failed (%s) — retrying in %.1fs",
                    attempt,
                    len(delays),
                    type(exc).__name__,
                    delay,
                )
                await _asyncio.sleep(delay)
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
         :func:`_insert_answer_run_with_retry` — 3 attempts with
         exponential backoff (0.5s, 1.0s, 2.0s) so transient asyncpg /
         PgBouncer / PG flaps don't silently lose lineage rows.
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

    from app.config import settings as _settings  # noqa: PLC0415
    from app.models.answer_run import (  # noqa: PLC0415
        normalize_backend as _normalize_backend,
    )

    citation_state = "rejected" if not state.response.citations else "committed"

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

    # FK-safety (option (b) from §39 follow-up). silver.answer_runs.project_id
    # has a FK to silver.projects. Real production callers occasionally pass
    # workspace UUIDs or stale project_ids that don't resolve — the resulting
    # ForeignKeyViolationError used to take down the whole persist (incl. the
    # trace row, because enqueue_trace was inside `if row is not None`).
    # Validate-then-NULL keeps the row alive at the cost of one cheap
    # SELECT — far cheaper than retrying the INSERT 3× and then losing the
    # trace forever. The FK column itself is nullable (ON DELETE SET NULL).
    if project_id is not None:
        try:
            async with pg_pool.acquire() as _fk_conn:
                _project_exists = await _fk_conn.fetchval(
                    "SELECT 1 FROM silver.projects WHERE project_id = $1::uuid",
                    project_id,
                )
            if _project_exists is None:
                logger.warning(
                    "agentic_retrieval.persist: project_id %s not present in "
                    "silver.projects — dropping to NULL on INSERT",
                    project_id,
                )
                project_id = None
        except Exception:
            # Don't let the FK pre-check fail the persist. If the SELECT
            # itself dies, fall through with the original project_id — the
            # retry helper will handle the eventual FK error.
            logger.debug(
                "agentic_retrieval.persist: project_id FK pre-check failed; "
                "trusting caller-supplied value",
                exc_info=True,
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

    try:
        row = await _insert_answer_run_with_retry(
            pg_pool,
            """
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
                output_tokens
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, 0, $5, $6, $7, $8::uuid,
                $9::jsonb, $10::jsonb, $11::jsonb, $12, $13, $14, $15, $16
            )
            RETURNING answer_run_id
            """,
            workspace_id,
            project_id,
            state.query,
            spec_query_class,
            citation_state,
            # The model that actually answered, not the one configured.
            # `llm_calls.record_run_llm_model()` stamps this inside the same
            # node as the LLM call and it rides here on the response object;
            # `settings.effective_llm_model` is only a fallback for runs that
            # produced no answer-bearing call. The identical bug on
            # `audit.query_audit_log.llm_model` was fixed on 2026-08-21 —
            # this was its second home.
            _answering_model,
            # Audit 2026-08-14 (finding 5): the agentic path previously
            # never wrote backend_used (silent NULL). Record the active
            # LLM backend, normalised onto the answer_runs_backend_valid
            # CHECK list so an unrecognised value persists as 'unknown'
            # instead of violating the constraint.
            _normalize_backend(getattr(_settings, "LLM_BACKEND", None)),
            cols["session_id"],
            _json.dumps(cols["lineage_retrieved_sources"]),
            _json.dumps(cols["lineage_filters_applied"]),
            _json.dumps(cols["lineage_qaqc_filters_applied"]),
            cols["answer_schema_version"],
            _response_confidence,
            _latency_ms,
            # These two columns have existed since 2026-04-21 and had never
            # been written: production held exactly one answer_runs row and
            # `count(input_tokens)` was 0. `llm_calls.py` even documents the
            # orchestrator as reading `get_run_token_usage()` "immediately
            # before the answer_runs INSERT" — the INSERT named 15 columns
            # and neither token column was among them.
            _input_tokens,
            _output_tokens,
        )

        if row is not None:
            _answer_run_id = str(row["answer_run_id"])

        if row and state.response is not None:
            from uuid import UUID as _UUID  # noqa: PLC0415
            try:
                state.response.answer_run_id = _UUID(str(row["answer_run_id"]))
            except Exception:
                # Pydantic assignment must never break observability.
                logger.debug(
                    "agentic_retrieval.persist: failed to stamp "
                    "answer_run_id on response",
                    exc_info=True,
                )

        # RetrievalInspector follow-up — also persist the retrieval +
        # citation children so the inspector's Retrieval / Context panels
        # have data to render. Best-effort: any failure logs + continues.
        _retr_count = 0
        _cite_count = 0
        if row is not None:
            try:
                _retr_count, _cite_count = await _persist_retrieval_and_citation_items(
                    pg_pool=pg_pool,
                    answer_run_id=str(row["answer_run_id"]),
                    workspace_id=workspace_id,
                    state=state,
                )
            except Exception:
                logger.exception(
                    "agentic_retrieval.persist: child-row INSERTs failed (non-fatal)"
                )

        # Plan §0e retrieval-trace observability — enqueue a RetrievalTrace
        # for the silver.query_traces buffer. Fire-and-forget: the writer
        # never raises, so this can't break the answer path.
        #
        # §39 follow-up (c): the trace is now emitted even when the
        # answer_runs INSERT failed (row is None). Previously this block
        # was gated on `if row is not None:`, which meant FK violations or
        # pool exhaustion that killed the row also killed observability.
        # `answer_run_id` falls through as None in that case — the
        # RetrievalTrace schema explicitly allows it (trace_writer.py L91).
        if True:  # noqa: SIM103 — see comment above; trace must run regardless
            try:
                from app.services.trace_writer import (  # noqa: PLC0415
                    GuardResults,
                    LatencyBreakdown,
                    RawResultsPerSource,
                    RetrievalTrace,
                    enqueue_trace,
                )

                # Per-source candidate counts — best-effort scrape from
                # the tool_results list. The shape varies by tool, so we
                # only count entries that come back as lists/tuples.
                _source_counts: dict[str, int] = {
                    "qdrant_dense": 0,
                    "qdrant_sparse": 0,
                    "postgis": 0,
                    "neo4j": 0,
                }
                for tool_name, tool_payload in state.tool_results:
                    if tool_name == "search_documents" and isinstance(tool_payload, list):
                        _source_counts["qdrant_dense"] += len(tool_payload)
                    elif tool_name == "traverse_knowledge_graph" and isinstance(
                        tool_payload, list
                    ):
                        _source_counts["neo4j"] += len(tool_payload)
                    elif tool_name in (
                        "query_spatial_collars",
                        "query_assay_data",
                        "query_downhole_logs",
                    ) and isinstance(tool_payload, list):
                        _source_counts["postgis"] += len(tool_payload)

                _candidate_total = sum(_source_counts.values())

                # Plan §3a/§3b wiring — prefer the typed EvidencePacket's
                # `kind` list when it's available; fall back to the legacy
                # tool-name list otherwise. The packet's order is canonical
                # (authority-ranked) so the trace shows what the assembler
                # actually read first.
                if state.evidence_packet is not None and state.evidence_packet.evidence:
                    _evidence_types = [e.kind for e in state.evidence_packet.evidence]
                else:
                    _evidence_types = [
                        name for name, _ in state.tool_results if name
                    ]

                _selected_groups = (
                    len(state.response.citations) if state.response is not None else 0
                )

                # Plan §3f — when the packet exists, its remaining_budget
                # is the truth (system_prompt_tokens + total_tokens already
                # subtracted). persist_node writes it directly to
                # silver.query_traces.remaining_context_budget so dashboards
                # can spot tight-budget queries before they fail.
                _remaining_context_budget: int | None = None
                if state.evidence_packet is not None:
                    _remaining_context_budget = state.evidence_packet.remaining_budget

                # Plan §4b foundation — typed guard error codes. Replaces
                # the prior string-prefix heuristic with the centralised
                # classifier in app.agent.guards. The trace stores the
                # enum values (".value" strings) for forward-compat with
                # the §4b repair-strategy dispatcher.
                from app.agent.guards import (  # noqa: PLC0415
                    GuardErrorCode,
                    classify_guards,
                )
                from app.agent.hallucination.citation_markers import (  # noqa: PLC0415
                    CITATION_MARKER_RE as _CITATION_MARKER_RE,
                )
                _conflicting = bool(
                    state.response is not None
                    and getattr(state.response, "conflicting_evidence", None)
                )
                _guard_codes: list[GuardErrorCode] = classify_guards(
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
                _guard_failure_codes = [c.value for c in _guard_codes]

                # Plan §4b — also stamp the typed codes onto the response
                # so the Laravel / React side has the data for the user-
                # facing renderer (Job 3). Mirror of the trace field; both
                # are best-effort and never fail the answer path.
                if state.response is not None:
                    try:
                        state.response.guard_error_codes = list(_guard_failure_codes)
                    except Exception:  # pragma: no cover — defensive
                        logger.debug(
                            "agentic_retrieval.persist: failed to stamp "
                            "guard_error_codes on response",
                            exc_info=True,
                        )

                # Plan §I — evidence.* + guards.* + card.* Sentry tags.
                # persist_node is the latest point with both the prepared
                # packet AND the classifier output in hand; per the spec
                # doc this is where the §6b stampers live. The card.*
                # tag (added §6b P6, 2026-05-29) tracks which inline
                # visualisation (if any) the response shipped.
                try:
                    from app.agent.sentry_tags import (  # noqa: PLC0415
                        stamp_card_type_tag,
                        stamp_evidence_tags,
                        stamp_guards_tags,
                    )
                    stamp_evidence_tags(state.evidence_packet)
                    stamp_guards_tags(list(_guard_failure_codes))
                    stamp_card_type_tag(
                        getattr(state.response, "viz_payload", None)
                        if state.response is not None
                        else None,
                    )
                except Exception:  # pragma: no cover — defensive
                    logger.debug(
                        "evidence/guards/card sentry stamp failed (non-fatal)",
                        exc_info=True,
                    )

                # Plan §3a/§3b — stamp the typed evidence packet onto the
                # response in `.model_dump()` form so the Laravel SSE
                # bridge can serialise it straight through to Chat.tsx.
                # Frontend reads `evidence_packet.evidence[].kind` to
                # dispatch per-kind cards (spatial → MapLibre, table →
                # table card, etc.). None-safe: missing packet leaves the
                # field at its default None.
                if state.response is not None and state.evidence_packet is not None:
                    try:
                        state.response.evidence_packet = (
                            state.evidence_packet.model_dump(mode="json")
                        )
                    except Exception:  # pragma: no cover — defensive
                        logger.debug(
                            "agentic_retrieval.persist: failed to stamp "
                            "evidence_packet on response",
                            exc_info=True,
                        )

                # Plan §3e — stamp the multi-turn resolution audit onto
                # the response so the Chat.tsx preview chip can render
                # "Interpreted as: …". Mirror of the JSONB trace field;
                # both writes are independent for forward-compat.
                if (
                    state.response is not None
                    and state.query_original is not None
                    and state.resolution_trace
                ):
                    try:
                        state.response.multi_turn_resolution = {
                            "original_query": state.query_original,
                            "rewritten_query": state.query,
                            "trace": list(state.resolution_trace),
                            "overall_confidence": state.resolution_confidence,
                        }
                    except Exception:  # pragma: no cover — defensive
                        logger.debug(
                            "agentic_retrieval.persist: failed to stamp "
                            "multi_turn_resolution on response",
                            exc_info=True,
                        )

                _guard_results = GuardResults(
                    numeric_grounding=GuardErrorCode.NUMERIC_GROUNDING_FAILED not in _guard_codes,
                    entity_grounding=GuardErrorCode.ENTITY_NOT_FOUND not in _guard_codes,
                    citation_completeness=GuardErrorCode.CITATION_INCOMPLETE not in _guard_codes,
                    refusal_triggered=citation_state == "rejected",
                )

                # Plan §0e denorm extras (audit follow-up) — populate
                # tool_plan, generated_filters, reranker_scores into the
                # payload so dashboard queries don't have to JSONB-parse.
                _tool_plan_str = (
                    ", ".join(state.retrieval_profile.primary_tools)
                    if state.retrieval_profile else None
                )
                _tool_calls_list = [
                    {"name": name, "result_kind": type(payload).__name__}
                    for name, payload in state.tool_results
                ]
                _generated_filters: dict[str, Any] = {}
                if state.retrieval_filters is not None:
                    try:
                        _generated_filters = (
                            state.retrieval_filters.model_dump(exclude_none=True)
                            if hasattr(state.retrieval_filters, "model_dump")
                            else dict(state.retrieval_filters.__dict__)
                        )
                    except Exception:  # pragma: no cover — defensive
                        _generated_filters = {}

                _trace = RetrievalTrace(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    # §39 follow-up (c) — answer_run_id may be None when
                    # the INSERT failed; the schema allows it.
                    answer_run_id=(row["answer_run_id"] if row is not None else None),
                    otel_trace_id=None,
                    user_query=state.query,
                    system_prompt_tokens=getattr(
                        state, "system_prompt_tokens_estimate", None
                    ),
                    remaining_context_budget=_remaining_context_budget,
                    router_decision=str(state.intent) if state.intent else None,
                    router_confidence=(
                        float(state.intent_result.confidence)
                        if state.intent_result is not None
                        else None
                    ),
                    effective_intent=(
                        str(state.effective_intent) if state.effective_intent else None
                    ),
                    tool_plan=_tool_plan_str,
                    tool_calls=_tool_calls_list,
                    generated_filters=_generated_filters,
                    raw_results_per_source=RawResultsPerSource(**_source_counts),
                    candidate_count_pre_rerank=_candidate_total or None,
                    selected_context_groups=_selected_groups or None,
                    evidence_types_in_context=_evidence_types,
                    guard_results=_guard_results,
                    guard_failure_codes=_guard_failure_codes,
                    # Plan §4b/§4c Stage 1 — repair_strategies_used picks up
                    # the shadow-mode planner output. In shadow mode the
                    # strategies are what the loop WOULD have attempted;
                    # in full mode they're what it DID attempt. The trace
                    # field name doesn't disambiguate (intentionally — it's
                    # the same observability surface for both modes).
                    repair_strategies_used=list(state.repair_strategy_history),
                    repair_attempts=len(state.repair_attempts),
                    death_loop_triggered=False,  # full loop will set this
                    cache_hit=False,
                    cache_type=None,
                    latency_ms=LatencyBreakdown(total=_latency_ms),
                    # Plan §3 context-prep audit — populated when
                    # assemble_node ran prepare_evidence_for_intent and
                    # stamped the result on state.context_prep_audit_payload.
                    # Falls through to None when the flag was off OR the
                    # packet was empty.
                    context_prep_audit=getattr(
                        state, "context_prep_audit_payload", None,
                    ),
                    # Plan §3e multi-turn resolution — when resolve_node
                    # rewrote the query, the trace + confidence are on
                    # state.resolution_trace + state.resolution_confidence.
                    # Compose into a single JSONB.
                    multi_turn_resolution=(
                        {
                            "original_query": state.query_original,
                            "rewritten_query": state.query,
                            "trace": list(state.resolution_trace),
                            "overall_confidence": state.resolution_confidence,
                        }
                        if state.query_original is not None
                        and state.resolution_trace
                        else None
                    ),
                )

                await enqueue_trace(pg_pool, _trace)
            except Exception:
                logger.warning(
                    "agentic_retrieval.persist: trace enqueue failed (non-fatal)",
                    exc_info=True,
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
            row["answer_run_id"] if row else None,
            _retr_count,
            _cite_count,
        )
    except Exception:
        # Terminal failure after 3 retries — escalate via structured log
        # (Alertmanager picks up extra={"alert": True}) and bump the
        # Prometheus counter so dashboards/PromQL can rate-alert. The
        # answer is already streamed back to the user so we keep this
        # path non-fatal — but the lineage row is permanently lost.
        try:
            from app.metrics import AGENTIC_PERSIST_FAILURES  # noqa: PLC0415

            AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs").inc()
        except Exception:  # pragma: no cover — never block on metrics
            logger.debug(
                "agentic_retrieval.persist: AGENTIC_PERSIST_FAILURES "
                "counter inc failed",
                exc_info=True,
            )
        logger.error(
            "agentic_retrieval.persist: answer_runs INSERT failed after retries",
            exc_info=True,
            extra={"alert": True},
        )

    # L1546 — meter the spend. Deliberately outside the try above: the
    # tokens were bought whether or not the lineage row landed, and a cost
    # record that vanishes whenever persistence fails is precisely the
    # blind spot this closes. `_answer_run_id` is None when the INSERT
    # never returned one; usage.usage_events.invocation_id is nullable.
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

    # Return the (possibly mutated) response so LangGraph propagates the
    # stamped answer_run_id back to the caller. Mutation-in-place would
    # also work because Pydantic models are reference types, but returning
    # explicitly makes the data flow obvious.
    return {"response": state.response} if state.response is not None else {}


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
            for r in retr_rows:
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
            for c in cite_rows:
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
