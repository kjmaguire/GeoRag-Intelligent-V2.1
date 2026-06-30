"""Live tests for the doc-phase 145 Hatchet workflow body graduations.

Tests the *task body* functions (not the Hatchet runtime — that needs a
running Hatchet engine). Hatchet's `Task` object exposes `.aio_mock_run()`
which invokes the underlying async function with a stub Context — the
canonical way to exercise a task body in unit tests.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.hatchet_workflows.generate_report import (
    GenerateReportInput,
)
from app.hatchet_workflows.generate_report import (
    execute as generate_report_execute,
)
from app.hatchet_workflows.score_targets import (
    ScoreTargetsInput,
)
from app.hatchet_workflows.score_targets import (
    execute as score_targets_execute,
)


async def _run_task_body(task, input_obj):
    """Invoke a Hatchet Task's body via aio_mock_run (public API).
    Returns the body's return value."""
    return await task.aio_mock_run(input_obj)


# ----------------------------------------------------------------------
# generate_report
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generate_report_task_body_runs_planning_pipeline():
    """Smoke: weekly_project_digest input → planning pipeline runs +
    output has partial-state counts populated."""
    inp = GenerateReportInput(
        workspace_id=uuid4(),
        project_id=uuid4(),
        report_type="weekly_project_digest",
        requested_by_user_id=1,
        export_request_id=uuid4(),
    )
    out = await _run_task_body(generate_report_execute, inp)
    assert out.success is True
    assert out.failure_reason is None
    assert out.planned_sections_count >= 3
    assert out.section_drafts_count == out.planned_sections_count
    assert out.evidence_items_count > 0
    # weekly_project_digest is R3 → no sign-off required.
    assert out.sign_off_required is False


@pytest.mark.asyncio
async def test_generate_report_task_body_marks_signoff_for_r5_reports():
    inp = GenerateReportInput(
        workspace_id=uuid4(),
        project_id=uuid4(),
        report_type="target_recommendation",  # R5
        requested_by_user_id=1,
        export_request_id=uuid4(),
    )
    out = await _run_task_body(generate_report_execute, inp)
    assert out.success is True
    assert out.sign_off_required is True
    assert out.sign_off_complete is False


@pytest.mark.asyncio
async def test_generate_report_task_body_runs_all_11_report_types():
    """Each of the 11 §15.2 report types completes the planning
    pipeline without failure."""
    report_types = [
        "weekly_project_digest", "ingestion_quality",
        "technical_due_diligence", "executive_project_intelligence",
        "gis_arcgis_sync", "target_recommendation", "public_geo_overlay",
        "data_room_package", "what_changed", "ni43101_section_pack",
        "csa11348_disclosure_pack",
    ]
    for rt in report_types:
        inp = GenerateReportInput(
            workspace_id=uuid4(),
            project_id=uuid4(),
            report_type=rt,  # type: ignore[arg-type]
            requested_by_user_id=1,
            export_request_id=uuid4(),
        )
        out = await _run_task_body(generate_report_execute, inp)
        assert out.success is True, f"{rt} failed: {out.failure_reason}"
        assert out.planned_sections_count > 0, f"{rt} had no sections"


# ----------------------------------------------------------------------
# score_targets
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_score_targets_task_body_with_zones_produces_ranked_output():
    inp = ScoreTargetsInput(
        workspace_id=uuid4(),
        project_id=uuid4(),
        requested_by_user_id=1,
        aoi_geom_wkt="POLYGON((-1 -1, 2 -1, 2 2, -1 2, -1 -1))",
        score_request_id=uuid4(),
        extra_candidate_zone_wkts=[
            "POLYGON((0 0, 0.5 0, 0.5 0.5, 0 0.5, 0 0))",
            "POLYGON((0.5 0, 1 0, 1 0.5, 0.5 0.5, 0.5 0))",
            "POLYGON((0 0.5, 0.5 0.5, 0.5 1, 0 1, 0 0.5))",
        ],
    )
    out = await _run_task_body(score_targets_execute, inp)
    assert out.success is True
    assert out.candidate_zone_count == 3
    assert out.recommended_target_count == 3
    assert out.failure_reason is None
    assert out.target_model_slug == "athabasca_uranium"  # default
    assert out.top_aggregate_score is not None
    assert 0.0 <= out.top_aggregate_score <= 1.0


@pytest.mark.asyncio
async def test_score_targets_task_body_with_commodity_hint_selects_model():
    inp = ScoreTargetsInput(
        workspace_id=uuid4(),
        project_id=uuid4(),
        requested_by_user_id=1,
        aoi_geom_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        target_commodity="Au",
        score_request_id=uuid4(),
        extra_candidate_zone_wkts=[
            "POLYGON((0 0, 0.5 0, 0.5 0.5, 0 0.5, 0 0))",
        ],
    )
    out = await _run_task_body(score_targets_execute, inp)
    assert out.success is True
    assert out.target_model_slug is not None
    assert "gold" in out.target_model_slug


@pytest.mark.asyncio
async def test_score_targets_task_body_with_no_zones_runs_with_synthesised_grid():
    """Phase H4 — `generate_candidate_zones` now synthesises a 5-zone
    stub grid when the caller passes no zones. So "no zones in" yields
    "5 zones out" end-to-end. (Real PostGIS zone generation lands when
    the spatial pipeline ships — at that point the count will reflect
    real geological domain intersections instead of the stub grid.)"""
    inp = ScoreTargetsInput(
        workspace_id=uuid4(),
        project_id=uuid4(),
        requested_by_user_id=1,
        aoi_geom_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        score_request_id=uuid4(),
    )
    out = await _run_task_body(score_targets_execute, inp)
    assert out.success is True
    assert out.candidate_zone_count == 5
    assert out.recommended_target_count == 5
    assert out.top_aggregate_score is not None
