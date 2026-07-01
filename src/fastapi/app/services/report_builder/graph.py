"""§15.1 Report Builder LangGraph wiring.

Phase G.3 — all 12 nodes graduated (4 planning + 8 production). Each
node short-circuits on `state.failure_reason` so a downstream node
can inspect partial state without crashing.

Pipeline:

    START → select_report_type
          → plan_sections
          → gather_evidence
          → verify_evidence_budget
          → generate_section_drafts
          → validate_claims
          → attach_citations
          → generate_maps_charts
          → build_appendix
          → compliance_check
          → geologist_approval
          → export_package
          → activepieces_delivery (now Kestra per ADR-0001)
          → END

Several nodes ship with minimal-viable bodies that defer heavy
implementations:
* `generate_section_drafts` — deterministic assembly from evidence;
  LLM narration deferred to a follow-up.
* `generate_maps_charts` — records requested map_kinds + chart_kinds;
  MapLibre static rendering deferred to §17.4.
* `export_package` — emits a self-contained markdown bundle as a
  data: URI; PDF/DOCX/XLSX renderers deferred.
* `activepieces_delivery` — log-only; Kestra dispatch deferred to §7.11.

Caller pattern:

    graph = build_report_builder_graph()
    final_state = await graph.ainvoke(initial_state)
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from app.services.report_builder.nodes import (
    activepieces_delivery,
    attach_citations,
    build_appendix,
    compliance_check,
    export_package,
    gather_evidence,
    generate_maps_charts,
    generate_section_drafts,
    geologist_approval,
    plan_sections,
    select_report_type,
    validate_claims,
    verify_evidence_budget,
)
from app.services.report_builder.state import ReportBuilderState

log = logging.getLogger("georag.report_builder.graph")


# Ordered pipeline — used by both the graph builder and by tests that
# want to assert the wiring shape without re-instantiating LangGraph.
_PIPELINE: list[tuple[str, object]] = [
    ("select_report_type", select_report_type),
    ("plan_sections", plan_sections),
    ("gather_evidence", gather_evidence),
    ("verify_evidence_budget", verify_evidence_budget),
    ("generate_section_drafts", generate_section_drafts),
    ("validate_claims", validate_claims),
    ("attach_citations", attach_citations),
    ("generate_maps_charts", generate_maps_charts),
    ("build_appendix", build_appendix),
    ("compliance_check", compliance_check),
    ("geologist_approval", geologist_approval),
    ("export_package", export_package),
    ("activepieces_delivery", activepieces_delivery),
]


def build_report_builder_graph(*, checkpointer: object | None = None):
    """Build + compile the §15.1 LangGraph pipeline with all 12 nodes.

    Returns a compiled graph object whose `.ainvoke(initial_state)`
    returns the final ReportBuilderState (as a dict per LangGraph
    semantics — the caller can rehydrate with
    `ReportBuilderState.model_validate(result)`).

    Phase 0 #P2.1 (2026-05-18) — checkpointer support.
    Compile-time `checkpointer` argument enables LangGraph's pause/resume
    + HITL durability. Defaults to MemorySaver (process-local) when
    nothing is passed; production callers (Phase 1+) should inject a
    PostgresSaver so graph state survives worker restart.
    """
    g: StateGraph = StateGraph(ReportBuilderState)

    for name, fn in _PIPELINE:
        g.add_node(name, fn)

    g.add_edge(START, _PIPELINE[0][0])
    for (prev_name, _), (next_name, _) in zip(_PIPELINE, _PIPELINE[1:], strict=False):
        g.add_edge(prev_name, next_name)
    g.add_edge(_PIPELINE[-1][0], END)

    if checkpointer is None:
        try:
            from langgraph.checkpoint.memory import MemorySaver  # noqa: PLC0415
            checkpointer = MemorySaver()
        except ImportError:
            # Older langgraph (<0.2) — checkpoint module path differed.
            # Fall back to no checkpointer rather than crashing on import.
            checkpointer = None

    compiled = g.compile(checkpointer=checkpointer) if checkpointer else g.compile()
    log.info(
        "report_builder.graph compiled with %d nodes; checkpointer=%s",
        len(_PIPELINE),
        type(checkpointer).__name__ if checkpointer else "none",
    )
    return compiled


__all__ = ["build_report_builder_graph"]
