"""LangGraph wiring for agentic retrieval — Phase 2 / Step 2.3.

Linear pipeline:

    START → classify → route → execute → assemble → validate → demote → END

Each node is async; LangGraph handles the await orchestration. The
top-level :func:`run_agentic_retrieval` is the entry point the orchestrator
calls instead of ``run_deterministic_rag`` when
``settings.AGENTIC_RETRIEVAL_V2_ENABLED=True``.

The compiled graph is cached at module load so we don't pay the build
cost on every query.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.agentic_retrieval.context_envelope import ContextEnvelope
from app.agent.agentic_retrieval.nodes import (
    assemble_node,
    classify_node,
    demote_node,
    execute_node,
    persist_node,
    repair_shadow_node,
    resolve_node,
    route_node,
    validate_node,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)


_PIPELINE: tuple[tuple[str, Any], ...] = (
    # Plan §3e — multi-turn rewrite runs FIRST so the classifier
    # sees the expanded query. No-op when MULTI_TURN_RESOLUTION_ENABLED
    # is False (default) or when state.history is empty.
    # See docs/architecture/multi_turn_resolution_spec.md §6.3.
    ("resolve", resolve_node),
    ("classify", classify_node),
    ("route", route_node),
    ("execute", execute_node),
    ("assemble", assemble_node),
    ("validate", validate_node),
    ("demote", demote_node),
    # Plan §4b/§4c Stage 1 — shadow-mode repair planner. Runs unconditionally
    # at graph-build time but is a no-op when REPAIR_LOOP_SHADOW_ENABLED=False
    # (the default). When the flag flips, it stamps repair_codes_observed +
    # repair_strategy_history + repair_terminal_reason onto state for the
    # persist node + trace writer to pick up. NEVER mutates the response.
    # See docs/architecture/repair_loop_spec.md §8 Stage 1 for the rollout.
    ("repair_shadow", repair_shadow_node),
    # Phase 4 follow-up — write the answer_runs lineage row. Best-effort;
    # failures are logged but don't fail the answer (the response has
    # already streamed back by this point).
    ("persist", persist_node),
)


def _build_graph() -> Any:
    """Construct and compile the LangGraph (no checkpointer — single-shot)."""
    g: StateGraph = StateGraph(AgenticRetrievalState)
    for name, fn in _PIPELINE:
        g.add_node(name, fn)
    g.add_edge(START, _PIPELINE[0][0])
    for (a, _), (b, _) in zip(_PIPELINE, _PIPELINE[1:], strict=False):
        g.add_edge(a, b)
    g.add_edge(_PIPELINE[-1][0], END)
    return g.compile()


@lru_cache(maxsize=1)
def get_compiled_graph() -> Any:
    """Return the singleton compiled graph (built lazily on first call)."""
    logger.info("agentic_retrieval: compiling LangGraph pipeline")
    return _build_graph()


async def run_agentic_retrieval(
    query: str,
    deps: Any,
    *,
    context_envelope: ContextEnvelope | None = None,
    history: list[Any] | None = None,
) -> GeoRAGResponse:
    """Top-level entry: classify the query, route, retrieve, answer.

    Mirrors the contract of ``run_deterministic_rag`` (one query in, one
    :class:`GeoRAGResponse` out) so the orchestrator can dispatch to
    either path purely on the feature flag.

    Args:
        query: Raw user query text.
        deps: ``AgentDeps`` bundle the legacy orchestrator hands in.
        context_envelope: Optional 12-field context. None = all fields
            unspecified; the envelope-router will surface that in the
            OIUR uncertainty section and lineage.
        history: Optional conversation history —
            ``list[ConversationTurn]``. When provided AND
            ``settings.MULTI_TURN_RESOLUTION_ENABLED`` is True, the
            resolve_node will rewrite ``query`` to expand pronouns +
            demonstratives + comparatives. See
            docs/architecture/multi_turn_resolution_spec.md.

    On any unexpected node failure the graph propagates the partial state
    forward; the assemble node always returns *some* response (even if
    it's a refusal-shaped one) so this function never returns None.
    """
    graph = get_compiled_graph()
    # RetrievalInspector follow-up — capture wall-clock at entry so
    # persist_node can stamp silver.answer_runs.latency_ms.
    import time as _time_for_latency  # noqa: PLC0415

    # Plan §I — workspace.id Sentry tag applies to every transaction
    # the agent emits. Best-effort: stamper no-ops when the SDK isn't
    # installed and swallows its own errors. Called at the entry so
    # spans from every downstream node carry the tag.
    try:
        from app.agent.sentry_tags import stamp_workspace_tag  # noqa: PLC0415
        stamp_workspace_tag(getattr(deps, "workspace_id", None))
    except Exception:  # pragma: no cover — defensive
        pass

    initial = AgenticRetrievalState(
        query=query,
        deps=deps,
        context_envelope=context_envelope,
        history=list(history) if history else [],
        run_start_monotonic=_time_for_latency.monotonic(),
    )
    # LangGraph's ainvoke accepts either a dict or the state model; the
    # report-builder graph passes the model directly. final is a dict.
    final = await graph.ainvoke(initial)
    response = final.get("response") if isinstance(final, dict) else None
    if response is None:
        # Extremely defensive — should never happen because assemble_node
        # always emits a response (even refusal-flavoured). Build a flat
        # refusal so the caller never has to handle None.
        from app.agent.response_assembler import assemble_response  # noqa: PLC0415
        response = assemble_response(
            "I was unable to generate a response for this query.",
            tool_results=[],
        )
    return response


__all__ = ["get_compiled_graph", "run_agentic_retrieval"]
