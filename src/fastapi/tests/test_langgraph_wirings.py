"""Live tests for the §15.1 + §18.2 LangGraph wirings (doc-phase 141).

Verifies that the compiled graphs run end-to-end through the
graduated nodes and produce the expected state mutations.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.report_builder import (
    ReportBuilderState,
    build_report_builder_graph,
)
from app.services.target_recommendation import (
    TargetRecommendationState,
    build_target_recommendation_graph,
)
from app.services.target_recommendation.state import CandidateZone


# ----------------------------------------------------------------------
# §15.1 Report Builder wiring
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_report_builder_graph_runs_end_to_end():
    """Compile + ainvoke the report-builder graph; verify the 4
    graduated nodes all ran."""
    graph = build_report_builder_graph()
    initial = ReportBuilderState(
        report_id=uuid4(),
        workspace_id=uuid4(),
        project_id=uuid4(),
        report_type="weekly_project_digest",
        risk_tier="R3",
        requested_by_user_id=1,
    )
    result = await graph.ainvoke(initial)
    # LangGraph returns a dict — rehydrate.
    final = ReportBuilderState.model_validate(result)
    assert final.failure_reason is None
    assert final.started_at is not None  # select_report_type ran
    assert len(final.sections_plan) > 0   # plan_sections ran
    assert len(final.section_drafts) > 0  # gather_evidence ran


@pytest.mark.asyncio
async def test_report_builder_graph_propagates_failure_reason():
    """Mismatched risk tier → failure_reason populated by node 1.
    Subsequent nodes still run (they return state unchanged on
    failure_reason) but the failure is preserved."""
    graph = build_report_builder_graph()
    initial = ReportBuilderState(
        report_id=uuid4(),
        workspace_id=uuid4(),
        project_id=uuid4(),
        report_type="weekly_project_digest",  # registered as R3
        risk_tier="R5",                        # mismatch
        requested_by_user_id=1,
    )
    result = await graph.ainvoke(initial)
    final = ReportBuilderState.model_validate(result)
    assert final.failure_reason is not None
    assert "risk_tier=R5 does not match" in final.failure_reason


# ----------------------------------------------------------------------
# §18.2 Target Recommendation wiring
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_target_recommendation_graph_runs_end_to_end():
    """Compile + ainvoke the target-recommendation graph against
    a caller-supplied set of candidate zones."""
    graph = build_target_recommendation_graph()
    zones = [
        CandidateZone(
            zone_id=uuid4(),
            geom_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        )
        for _ in range(4)
    ]
    initial = TargetRecommendationState(
        run_id=uuid4(),
        workspace_id=uuid4(),
        project_id=uuid4(),
        requested_by_user_id=1,
        aoi_geom_wkt="POLYGON((-1 -1, 2 -1, 2 2, -1 2, -1 -1))",
        candidate_zones=zones,
    )
    result = await graph.ainvoke(initial)
    final = TargetRecommendationState.model_validate(result)

    # Pipeline produced ranked targets, sorted DESC by score.
    assert len(final.ranked_targets) == len(zones)
    scores = [t.aggregate_score for t in final.ranked_targets]
    assert scores == sorted(scores, reverse=True)
    # Phase H4 explain_score_factors now renders the deterministic
    # Markdown template (replacing the doc-phase 138 placeholder text
    # from rank_targets). Verify the new format markers are present.
    for t in final.ranked_targets:
        assert f"Rank #{t.rank}" in t.explanation_markdown
        assert "| Factor | Value | Weight | Contribution |" in t.explanation_markdown
        assert "Top contributing factor" in t.explanation_markdown

    # Uncertainties + deposit model selection both ran.
    assert len(final.uncertainties) == len(zones)
    assert final.target_model_id is not None
    assert final.workspace_playbook.get("selected_deposit_model_slug")


@pytest.mark.asyncio
async def test_target_recommendation_graph_with_no_zones_still_runs():
    """Even with empty candidate_zones, the graph runs cleanly to END
    Phase H4 — `generate_candidate_zones` now synthesises a 5-zone
    grid when the caller passes no zones (deterministic stub). So
    "no zones" runs end-to-end and yields 5 ranked targets, not 0."""
    graph = build_target_recommendation_graph()
    initial = TargetRecommendationState(
        run_id=uuid4(),
        workspace_id=uuid4(),
        project_id=uuid4(),
        requested_by_user_id=1,
        aoi_geom_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        candidate_zones=[],
    )
    result = await graph.ainvoke(initial)
    final = TargetRecommendationState.model_validate(result)
    assert final.failure_reason is None
    # generate_candidate_zones populated 5 synthetic zones
    assert len(final.candidate_zones) == 5
    assert len(final.scores) == 5
    assert len(final.ranked_targets) == 5
    assert final.target_model_id is not None  # node 1 still ran
    assert final.sent_to_review_cockpit is True  # routed at the end
