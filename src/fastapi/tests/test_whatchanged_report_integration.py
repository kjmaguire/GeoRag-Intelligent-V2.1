"""Live tests for the doc-phase 156 §7.2 ↔ §9.13 integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.hatchet_workflows.generate_report import (
    GenerateReportInput,
    execute as generate_report_execute,
)
from app.services.report_builder.nodes import gather_evidence, plan_sections
from app.services.report_builder.state import ReportBuilderState
from app.services.report_builder.whatchanged_integration import (
    gather_evidence_what_changed,
)


def _make_state(
    report_type: str = "what_changed",
    risk_tier: str = "R3",
    workspace_id: UUID | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> ReportBuilderState:
    return ReportBuilderState(
        report_id=uuid4(),
        workspace_id=workspace_id or UUID("a0000000-0000-0000-0000-000000000001"),
        project_id=uuid4(),
        report_type=report_type,  # type: ignore[arg-type]
        risk_tier=risk_tier,  # type: ignore[arg-type]
        requested_by_user_id=1,
        report_window_start=window_start,
        report_window_end=window_end,
    )


@pytest.mark.asyncio
async def test_what_changed_integration_returns_none_for_other_report_types():
    """Non-what_changed report → integration returns None (gather_evidence
    falls back to the synthetic stub)."""
    state = _make_state(report_type="weekly_project_digest")
    out = await gather_evidence_what_changed(state)
    assert out is None


@pytest.mark.asyncio
async def test_what_changed_integration_returns_none_without_window():
    """what_changed report without window → None (caller falls back)."""
    state = _make_state(report_type="what_changed")
    out = await gather_evidence_what_changed(state)
    assert out is None


@pytest.mark.asyncio
async def test_what_changed_integration_returns_drafts_with_window():
    """what_changed + window → 4 section drafts with real workspace deltas."""
    now = datetime.now(timezone.utc)
    state = _make_state(
        report_type="what_changed",
        window_start=now - timedelta(days=7),
        window_end=now,
    )
    drafts = await gather_evidence_what_changed(state)
    assert drafts is not None
    assert len(drafts) == 4
    section_ids = [d.section_id for d in drafts]
    assert section_ids == ["period", "data_changes", "claim_changes", "target_changes"]

    # period section body mentions the window dates.
    period_body = drafts[0].body_markdown
    assert "Reporting Period" in period_body
    assert "Start" in period_body and "End" in period_body

    # data_changes carries real-data evidence (Default Workspace has 9 hypotheses + 6 tickets from prior ticks).
    data_changes_body = drafts[1].body_markdown
    assert "Data Changes" in data_changes_body
    assert "Hypotheses generated" in data_changes_body


@pytest.mark.asyncio
async def test_what_changed_integration_threaded_through_gather_evidence():
    """End-to-end via gather_evidence node — what_changed report
    populates section_drafts from the detector."""
    now = datetime.now(timezone.utc)
    state = _make_state(
        report_type="what_changed",
        window_start=now - timedelta(days=7),
        window_end=now,
    )
    state = await plan_sections(state)  # seed sections_plan
    state = await gather_evidence(state)

    assert len(state.section_drafts) == 4
    assert state.failure_reason is None
    # Sanity-check: real data text appears in data_changes body.
    data_changes_draft = next(
        d for d in state.section_drafts if d.section_id == "data_changes"
    )
    assert "Hypotheses generated" in data_changes_draft.body_markdown


@pytest.mark.asyncio
async def test_what_changed_falls_back_to_stub_without_window():
    """No window → falls back to synthetic stub (covers all 4 sections)."""
    state = _make_state(report_type="what_changed", window_start=None, window_end=None)
    state = await plan_sections(state)
    state = await gather_evidence(state)

    # 4 section drafts (matches template), but they're stub claims.
    assert len(state.section_drafts) == 4
    # Stub body carries the synthetic_stub tag.
    assert "synthetic_stub" in state.section_drafts[0].body_markdown


@pytest.mark.asyncio
async def test_generate_report_task_body_threads_window_through():
    """Full Hatchet body invocation with window → ReportBuilderState
    carries the window + the integration runs end-to-end."""
    now = datetime.now(timezone.utc)
    inp = GenerateReportInput(
        workspace_id=UUID("a0000000-0000-0000-0000-000000000001"),
        project_id=uuid4(),
        report_type="what_changed",
        requested_by_user_id=1,
        export_request_id=uuid4(),
        report_window_start_iso=(now - timedelta(days=7)).isoformat(),
        report_window_end_iso=now.isoformat(),
    )
    out = await generate_report_execute.aio_mock_run(inp)
    assert out.success is True
    assert out.section_drafts_count == 4
    # 4 sections * at least 1 evidence item each = ≥4 evidence items.
    assert out.evidence_items_count >= 4
