"""score_targets Hatchet workflow (§8.6 / §18.2).

Doc-phase 88 skeleton → doc-phase 145 graduation.

Wraps the §18.2 Target Recommendation LangGraph (wired in doc-phase 141)
in a durable Hatchet workflow.

Today's task body invokes the **6 graduated nodes** (per doc-phase 138):

  select_commodity_deposit_model → load_workspace_playbook →
  score_candidate_zones (§8.7 weighted formula — real math) →
  calculate_uncertainty → apply_constraints → rank_targets

The 6 still-skeleton nodes (collect_private_evidence,
collect_public_geoscience, generate_candidate_zones,
explain_score_factors, create_map_layers, route_to_review_cockpit)
are not in the wired graph yet. The caller (Laravel queue or
Kestra flow) must pre-populate `candidate_zones` via
`extra_candidate_zone_wkts` — when `generate_candidate_zones`
graduates the wiring inserts it before scoring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.target_recommendation import build_target_recommendation_graph
from app.services.target_recommendation.state import (
    CandidateZone,
    ScoringKind,
    TargetRecommendationState,
)

log = logging.getLogger("georag.hatchet.score_targets")


# =============================================================================
# Input + output models
# =============================================================================
class ScoreTargetsInput(BaseModel):
    """Trigger payload from Laravel queue or Kestra flow."""

    workspace_id: UUID
    project_id: UUID
    requested_by_user_id: int
    aoi_geom_wkt: str = Field(
        ...,
        description="Project AOI polygon (PostGIS WKT, EPSG:4326).",
    )
    target_model_slug: str | None = Field(
        default=None,
        description="Optional pin to a specific deposit model. If null, the "
                    "agent chooses based on project commodity_primary.",
    )
    target_commodity: str | None = Field(
        default=None,
        description="Optional commodity hint (e.g. 'U', 'Au', 'Cu') used by "
                    "select_commodity_deposit_model when target_model_slug "
                    "is unset.",
    )
    scoring_kind: ScoringKind = Field(
        default="weighted",
        description="weighted (Phase 8 default) | xgboost (Phase 12) | ensemble",
    )
    score_request_id: UUID = Field(
        ...,
        description="UUID for R3+ idempotency keying. Same key = same run.",
    )
    # Doc-phase 145 — caller-supplied candidate zone polygons. When
    # `generate_candidate_zones` graduates this becomes optional (the
    # node generates zones from the AOI + evidence layers).
    extra_candidate_zone_wkts: list[str] = Field(
        default_factory=list,
        description="WKT polygons for pre-known candidate zones. Required "
                    "until generate_candidate_zones graduates from skeleton.",
    )


class ScoreTargetsOutput(BaseModel):
    """Final result of the workflow."""

    run_id: UUID
    success: bool
    candidate_zone_count: int = 0
    recommended_target_count: int = 0
    target_heatmap_layer_uri: str | None = None
    ranked_targets_layer_uri: str | None = None
    sent_to_review_cockpit: bool = False
    review_cockpit_url: str | None = None
    failure_stage: str | None = None
    failure_reason: str | None = None
    # Doc-phase 145 — partial-state passthrough.
    target_model_slug: str | None = None
    top_aggregate_score: float | None = None


# =============================================================================
# Workflow registration
# =============================================================================
score_targets = hatchet.workflow(
    name="score_targets",
    input_validator=ScoreTargetsInput,
)


# =============================================================================
# The workflow task
# =============================================================================
@score_targets.task(execution_timeout="24h", retries=0)
async def execute(input: ScoreTargetsInput, ctx: Context) -> ScoreTargetsOutput:
    """Run the §18.2 Target Recommendation Graph (graduated half).

    Doc-phase 145 graduation. The 6 graduated nodes run via the
    doc-phase 141 LangGraph wiring; the 6 still-skeleton nodes
    are not in the wired graph yet.
    """
    run_id = uuid4()

    log.info(
        "score_targets.task_started run_id=%s workspace=%s project=%s "
        "scoring_kind=%s n_zones=%d",
        run_id, input.workspace_id, input.project_id,
        input.scoring_kind, len(input.extra_candidate_zone_wkts),
    )

    # Build the playbook hints (target_commodity + target_model_slug pin).
    playbook: dict[str, Any] = {}
    if input.target_commodity:
        playbook["target_commodity"] = input.target_commodity
    if input.target_model_slug:
        playbook["pinned_deposit_model_slug"] = input.target_model_slug

    # Caller-supplied candidate zones (will be replaced by the
    # generate_candidate_zones node when it graduates).
    zones = [
        CandidateZone(zone_id=uuid4(), geom_wkt=wkt)
        for wkt in input.extra_candidate_zone_wkts
    ]

    initial_state = TargetRecommendationState(
        run_id=run_id,
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        requested_by_user_id=input.requested_by_user_id,
        aoi_geom_wkt=input.aoi_geom_wkt,
        scoring_kind=input.scoring_kind,
        candidate_zones=zones,
        workspace_playbook=playbook,
        started_at=datetime.now(timezone.utc),
    )

    graph = build_target_recommendation_graph()
    raw = await graph.ainvoke(initial_state)
    final = TargetRecommendationState.model_validate(raw)

    success = final.failure_reason is None
    failure_stage: str | None = None
    if not success:
        for marker, stage in [
            ("select_commodity_deposit_model", "select_commodity_deposit_model"),
            ("load_workspace_playbook", "load_workspace_playbook"),
            ("score_candidate_zones", "score_candidate_zones"),
            ("calculate_uncertainty", "calculate_uncertainty"),
            ("apply_constraints", "apply_constraints"),
            ("rank_targets", "rank_targets"),
        ]:
            if marker in (final.failure_reason or ""):
                failure_stage = stage
                break

    top_score = None
    if final.ranked_targets:
        top_score = final.ranked_targets[0].aggregate_score

    log.info(
        "score_targets.task_completed run_id=%s success=%s "
        "candidate_zones=%d ranked_targets=%d top_score=%s",
        run_id, success,
        len(final.candidate_zones), len(final.ranked_targets), top_score,
    )

    # Broadcast workspace.data_updated so Foundry/Targets re-fetches
    # targeting.target_recommendations. Best-effort — broadcast failure
    # must not fail the workflow. Only fires on success (failed runs
    # didn't write recommendations the page would read).
    if success:
        try:
            from app.services.laravel_bridge import post_workspace_data_updated
            await post_workspace_data_updated(
                workspace_id=str(input.workspace_id),
                project_id=str(input.project_id),
                pipeline_run_id=str(run_id),
                affected_types=["targets"],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "score_targets: workspace.data_updated broadcast failed "
                "run_id=%s err=%s", run_id, exc,
            )

    # Phase 2 admin surface push — three surfaces fire from this workflow
    # regardless of success/failure (operators need to see failed runs too):
    #
    #   1. workflow-runs   — append to Admin/WorkflowRuns + Admin/HatchetWorkers
    #   2. target-recommendation — refresh Admin/TargetRecommendationRuns list
    #   3. target-run.{run_id}   — refresh Admin/TargetRecommendationCockpit
    #                              (per-resource drilldown — matches the
    #                              admin.reports.{build_id} precedent)
    try:
        from app.services.laravel_bridge import post_admin_surface_updated
        terminal_status = "success" if success else "failure"
        admin_payload = {
            "workflow_kind": "score_targets",
            "run_id": str(run_id),
            "workspace_id": str(input.workspace_id),
            "project_id": str(input.project_id),
            "status": terminal_status,
            "failure_reason": final.failure_reason if not success else None,
        }
        await post_admin_surface_updated(
            surface="workflow-runs",
            affected_props=["workflow_runs"],
            payload=admin_payload,
        )
        await post_admin_surface_updated(
            surface="target-recommendation",
            affected_props=["runs"],
            payload=admin_payload,
        )
        await post_admin_surface_updated(
            surface="target-run",
            surface_id=str(run_id),
            affected_props=["run"],
            payload=admin_payload,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "score_targets: admin surface broadcasts failed run_id=%s err=%s",
            run_id, exc,
        )

    return ScoreTargetsOutput(
        run_id=run_id,
        success=success,
        candidate_zone_count=len(final.candidate_zones),
        recommended_target_count=len(final.ranked_targets),
        failure_stage=failure_stage,
        failure_reason=final.failure_reason,
        target_model_slug=final.workspace_playbook.get("selected_deposit_model_slug"),
        top_aggregate_score=top_score,
    )
