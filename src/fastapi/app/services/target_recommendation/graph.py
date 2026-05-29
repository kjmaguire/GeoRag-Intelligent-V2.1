"""§18.2 Target Recommendation LangGraph wiring.

Phase H4 — graduates the remaining 5 nodes (collect_private_evidence,
collect_public_geoscience, generate_candidate_zones, create_map_layers,
route_to_review_cockpit) into the wired pipeline. All 12 §18.2 nodes
are now in the graph.

Wired pipeline:

    START
      → select_commodity_deposit_model
      → load_workspace_playbook
      → collect_private_evidence
      → collect_public_geoscience
      → generate_candidate_zones      (no-op if caller pre-populated zones)
      → score_candidate_zones
      → calculate_uncertainty
      → apply_constraints
      → rank_targets
      → explain_score_factors
      → create_map_layers
      → route_to_review_cockpit
      → END

Caller pattern:

    graph = build_target_recommendation_graph()
    final_state_dict = await graph.ainvoke(initial_state)
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from app.services.target_recommendation.nodes import (
    apply_constraints,
    calculate_uncertainty,
    collect_private_evidence,
    collect_public_geoscience,
    create_map_layers,
    explain_score_factors,
    generate_candidate_zones,
    load_workspace_playbook,
    rank_targets,
    route_to_review_cockpit,
    score_candidate_zones,
    select_commodity_deposit_model,
)
from app.services.target_recommendation.state import TargetRecommendationState

log = logging.getLogger("georag.target_recommendation.graph")


def build_target_recommendation_graph(*, checkpointer: object | None = None):
    """Build + compile the §18.2 LangGraph pipeline with all 12 nodes.

    Phase 0 #P2.1 (2026-05-18) — checkpointer support. `checkpointer`
    defaults to MemorySaver; pass a PostgresSaver in production for
    cross-worker durability + HITL pause/resume.
    """
    g: StateGraph = StateGraph(TargetRecommendationState)

    g.add_node("select_commodity_deposit_model", select_commodity_deposit_model)
    g.add_node("load_workspace_playbook", load_workspace_playbook)
    g.add_node("collect_private_evidence", collect_private_evidence)
    g.add_node("collect_public_geoscience", collect_public_geoscience)
    g.add_node("generate_candidate_zones", generate_candidate_zones)
    g.add_node("score_candidate_zones", score_candidate_zones)
    g.add_node("calculate_uncertainty", calculate_uncertainty)
    g.add_node("apply_constraints", apply_constraints)
    g.add_node("rank_targets", rank_targets)
    g.add_node("explain_score_factors", explain_score_factors)
    g.add_node("create_map_layers", create_map_layers)
    g.add_node("route_to_review_cockpit", route_to_review_cockpit)

    g.add_edge(START, "select_commodity_deposit_model")
    g.add_edge("select_commodity_deposit_model", "load_workspace_playbook")
    g.add_edge("load_workspace_playbook", "collect_private_evidence")
    g.add_edge("collect_private_evidence", "collect_public_geoscience")
    g.add_edge("collect_public_geoscience", "generate_candidate_zones")
    g.add_edge("generate_candidate_zones", "score_candidate_zones")
    g.add_edge("score_candidate_zones", "calculate_uncertainty")
    g.add_edge("calculate_uncertainty", "apply_constraints")
    g.add_edge("apply_constraints", "rank_targets")
    g.add_edge("rank_targets", "explain_score_factors")
    g.add_edge("explain_score_factors", "create_map_layers")
    g.add_edge("create_map_layers", "route_to_review_cockpit")
    g.add_edge("route_to_review_cockpit", END)

    if checkpointer is None:
        try:
            from langgraph.checkpoint.memory import MemorySaver  # noqa: PLC0415
            checkpointer = MemorySaver()
        except ImportError:
            checkpointer = None

    compiled = g.compile(checkpointer=checkpointer) if checkpointer else g.compile()
    log.info(
        "target_recommendation.graph compiled with 12 graduated nodes; "
        "checkpointer=%s",
        type(checkpointer).__name__ if checkpointer else "none",
    )
    return compiled


__all__ = ["build_target_recommendation_graph"]
