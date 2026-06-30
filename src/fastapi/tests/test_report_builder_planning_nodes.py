"""Live tests for the §7-A v1 report_builder planning nodes (doc-phase 137).

Covers the first 4 of 12 §15.1 nodes — the "planning half" that runs
without an LLM:
  1. select_report_type
  2. plan_sections
  3. gather_evidence
  4. verify_evidence_budget

Pure unit tests against the Pydantic state model — no DB required.
"""
from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import pytest

from app.services.report_builder.nodes import (
    gather_evidence,
    plan_sections,
    select_report_type,
    verify_evidence_budget,
)
from app.services.report_builder.state import (
    Claim,
    ReportBuilderState,
    SectionDraft,
)


def _make_state(
    report_type: str = "weekly_project_digest",
    risk_tier: str = "R3",
) -> ReportBuilderState:
    return ReportBuilderState(
        report_id=uuid4(),
        workspace_id=uuid4(),
        project_id=uuid4(),
        report_type=report_type,  # type: ignore[arg-type]
        risk_tier=risk_tier,  # type: ignore[arg-type]
        requested_by_user_id=1,
    )


# ----------------------------------------------------------------------
# select_report_type
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_select_report_type_happy_path():
    state = _make_state("weekly_project_digest", "R3")
    out = await select_report_type(state)
    assert out.failure_reason is None
    assert out.started_at is not None


@pytest.mark.asyncio
async def test_select_report_type_rejects_mismatched_tier():
    # weekly_project_digest is R3, not R5.
    state = _make_state("weekly_project_digest", "R5")
    out = await select_report_type(state)
    assert out.failure_reason is not None
    assert "risk_tier=R5 does not match" in out.failure_reason


@pytest.mark.asyncio
async def test_select_report_type_preserves_started_at_if_set():
    from datetime import datetime
    pinned = datetime(2024, 1, 1, tzinfo=UTC)
    state = _make_state("weekly_project_digest", "R3").model_copy(
        update={"started_at": pinned}
    )
    out = await select_report_type(state)
    assert out.started_at == pinned


# ----------------------------------------------------------------------
# plan_sections
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_plan_sections_seeds_from_template():
    state = _make_state("weekly_project_digest", "R3")
    out = await plan_sections(state)
    assert len(out.sections_plan) >= 3  # weekly_project_digest has 4 sections
    assert out.failure_reason is None
    # Section ids are stable identifiers.
    ids = [s.section_id for s in out.sections_plan]
    assert "summary" in ids
    assert "recent_findings" in ids


@pytest.mark.asyncio
async def test_plan_sections_is_idempotent():
    state = _make_state("weekly_project_digest", "R3")
    after_first = await plan_sections(state)
    n = len(after_first.sections_plan)
    after_second = await plan_sections(after_first)
    assert len(after_second.sections_plan) == n


# ----------------------------------------------------------------------
# gather_evidence (synthetic stub)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gather_evidence_creates_drafts_for_each_section():
    state = _make_state("weekly_project_digest", "R3")
    state = await plan_sections(state)
    state = await gather_evidence(state)

    assert len(state.section_drafts) == len(state.sections_plan)
    # Every draft has at least one claim with at least one evidence item.
    for draft in state.section_drafts:
        assert draft.claims, f"section {draft.section_id} has no claims"
        for c in draft.claims:
            assert c.evidence, f"claim {c.claim_id} has no evidence"
            assert c.evidence[0].source_chunk_id.startswith("stub_chunk__")
        # Synthetic stub tag is present.
        assert "synthetic_stub" in draft.body_markdown


@pytest.mark.asyncio
async def test_gather_evidence_is_idempotent():
    state = _make_state("weekly_project_digest", "R3")
    state = await plan_sections(state)
    once = await gather_evidence(state)
    twice = await gather_evidence(once)
    assert len(twice.section_drafts) == len(once.section_drafts)


# ----------------------------------------------------------------------
# verify_evidence_budget
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_evidence_budget_passes_well_seeded():
    state = _make_state("weekly_project_digest", "R3")
    state = await plan_sections(state)
    state = await gather_evidence(state)
    out = await verify_evidence_budget(state)
    assert out.failure_reason is None


@pytest.mark.asyncio
async def test_verify_evidence_budget_fails_empty_drafts():
    state = _make_state("weekly_project_digest", "R3")
    out = await verify_evidence_budget(state)
    assert out.failure_reason is not None
    assert "empty" in out.failure_reason


@pytest.mark.asyncio
async def test_verify_evidence_budget_fails_under_evidenced_section():
    state = _make_state("weekly_project_digest", "R3")
    # Manually seed a section_draft with zero evidence.
    state = state.model_copy(update={
        "section_drafts": [
            SectionDraft(
                section_id="empty_section",
                body_markdown="",
                claims=[
                    Claim(
                        claim_id="empty_section.claim_1",
                        section_id="empty_section",
                        text="no-evidence claim",
                        evidence=[],
                    )
                ],
            )
        ]
    })
    out = await verify_evidence_budget(state, min_evidence_per_section=1)
    assert out.failure_reason is not None
    assert "under-evidenced" in out.failure_reason


# ----------------------------------------------------------------------
# Full planning pipeline integration
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_full_planning_pipeline_runs_clean():
    """Chain select_report_type → plan_sections → gather_evidence →
    verify_evidence_budget for each of the 11 report types."""

    report_types_and_tiers = [
        ("weekly_project_digest", "R3"),
        ("ingestion_quality", "R3"),
        ("technical_due_diligence", "R4"),
        ("executive_project_intelligence", "R4"),
        ("gis_arcgis_sync", "R3"),
        ("target_recommendation", "R5"),
        ("public_geo_overlay", "R3"),
        ("data_room_package", "R5"),
        ("what_changed", "R3"),
        ("ni43101_section_pack", "R5"),
        ("csa11348_disclosure_pack", "R5"),
    ]
    for rt, tier in report_types_and_tiers:
        state = _make_state(rt, tier)
        state = await select_report_type(state)
        assert state.failure_reason is None, f"{rt} select failed"
        state = await plan_sections(state)
        assert state.failure_reason is None, f"{rt} plan failed"
        assert state.sections_plan, f"{rt} has no sections"
        state = await gather_evidence(state)
        assert state.failure_reason is None, f"{rt} gather failed"
        state = await verify_evidence_budget(state)
        assert state.failure_reason is None, (
            f"{rt} verify failed: {state.failure_reason}"
        )
