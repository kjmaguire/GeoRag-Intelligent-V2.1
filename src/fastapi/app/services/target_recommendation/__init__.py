"""Target Recommendation Graph (§8.4 / §18.2).

The LangGraph pipeline that produces ranked target zones for a project
AOI. Twelve nodes per §18.2:

    select_commodity_deposit_model → load_workspace_playbook →
    collect_private_evidence → collect_public_geoscience →
    generate_candidate_zones → score_candidate_zones →
    calculate_uncertainty → apply_constraints →
    rank_targets → explain_score_factors →
    create_map_layers → route_to_review_cockpit

Phase H4 — all 12 nodes are graduated with deterministic
implementations. The graph compiles + runs end-to-end. Real
retrieval / PostGIS / SeaweedFS / Hatchet pause-resume hookups
plug into the existing nodes (the node signatures are stable).

This module exposes:
- `TargetRecommendationState` — Pydantic graph state.
- The twelve graduated node functions.
- `build_target_recommendation_graph()` — compiles the LangGraph
  pipeline. Invoked from the Hatchet `score_targets` workflow (§8.6).
"""
from app.services.target_recommendation.deposit_models import (
    DEPOSIT_MODEL_BY_SLUG,
    DEPOSIT_MODEL_TEMPLATES,
    get_deposit_model_template,
)
from app.services.target_recommendation.state import (
    TargetRecommendationState,
    ScoringKind,
)
from app.services.target_recommendation.nodes import (
    select_commodity_deposit_model,
    load_workspace_playbook,
    collect_private_evidence,
    collect_public_geoscience,
    generate_candidate_zones,
    score_candidate_zones,
    calculate_uncertainty,
    apply_constraints,
    rank_targets,
    explain_score_factors,
    create_map_layers,
    route_to_review_cockpit,
)

from app.services.target_recommendation.graph import (
    build_target_recommendation_graph,
)

__all__ = [
    "TargetRecommendationState",
    "ScoringKind",
    "DEPOSIT_MODEL_BY_SLUG",
    "DEPOSIT_MODEL_TEMPLATES",
    "get_deposit_model_template",
    "select_commodity_deposit_model",
    "load_workspace_playbook",
    "collect_private_evidence",
    "collect_public_geoscience",
    "generate_candidate_zones",
    "score_candidate_zones",
    "calculate_uncertainty",
    "apply_constraints",
    "rank_targets",
    "explain_score_factors",
    "create_map_layers",
    "route_to_review_cockpit",
    "build_target_recommendation_graph",
]
