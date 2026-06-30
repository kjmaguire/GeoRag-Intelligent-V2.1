"""generate_report Hatchet workflow (§7.10 / §15.1).

Doc-phase 83 skeleton → doc-phase 145 graduation.

Wraps the §15.1 Report Builder LangGraph (wired in doc-phase 141) in a
durable Hatchet workflow.

Today's task body invokes the **planning half** of the §15.1 pipeline
(4 of 12 nodes graduated; see doc-phase 137):

  select_report_type → plan_sections → gather_evidence → verify_evidence_budget

The remaining 8 nodes (LLM draft, claim validators, citation attachment,
map/chart rendering, appendix, compliance, sign-off, export, delivery)
are still skeleton. When they graduate the §15.1 wiring extends and
this workflow body keeps working without changes — `state.failure_reason`
catches under-evidenced sections, and the output model already exposes
the partial-state fields (sections_plan, section_drafts) needed for
the admin / observability surfaces.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.report_builder import build_report_builder_graph
from app.services.report_builder.state import (
    ReportBuilderState,
    ReportType,
)
from app.services.report_builder.templates import REPORT_RISK_TIERS

log = logging.getLogger("georag.hatchet.generate_report")


# =============================================================================
# Input + output models
# =============================================================================
class GenerateReportInput(BaseModel):
    """Trigger payload from Laravel queue or Kestra flow."""

    workspace_id: UUID
    project_id: UUID
    report_type: ReportType
    requested_by_user_id: int = Field(
        ..., description="public.users.id of the requesting user"
    )
    export_request_id: UUID = Field(
        ...,
        description="UUID for R3+ idempotency keying. Same key = same report.",
    )
    delivery_targets: list[str] = Field(
        default_factory=list,
        description="Optional delivery targets (email, teams, sharepoint_url).",
    )
    report_window_start_iso: str | None = Field(
        default=None,
        description="Optional reporting period start (ISO 8601). Required for "
                    "weekly_project_digest + what_changed report types.",
    )
    report_window_end_iso: str | None = Field(
        default=None,
        description="Optional reporting period end (ISO 8601).",
    )


class GenerateReportOutput(BaseModel):
    """Final result of the workflow."""

    report_id: UUID
    success: bool
    pdf_uri: str | None = None
    docx_uri: str | None = None
    xlsx_uri: str | None = None
    citation_manifest_uri: str | None = None
    source_manifest_uri: str | None = None
    evidence_json_uri: str | None = None
    hash_chain_proof_uri: str | None = None
    sign_off_required: bool = False
    sign_off_complete: bool = False
    delivery_dispatched: bool = False
    failure_stage: str | None = None
    failure_reason: str | None = None
    # Doc-phase 145 — partial-state passthrough (graduated planning nodes).
    planned_sections_count: int = 0
    section_drafts_count: int = 0
    evidence_items_count: int = 0


# =============================================================================
# Workflow registration
# =============================================================================
generate_report = hatchet.workflow(
    name="generate_report",
    input_validator=GenerateReportInput,
)


# =============================================================================
# The workflow task
# =============================================================================
@generate_report.task(execution_timeout="24h", retries=0)
async def execute(input: GenerateReportInput, ctx: Context) -> GenerateReportOutput:
    """Run the §15.1 Report Builder Graph (graduated planning half).

    Doc-phase 145 graduation. The 4 graduated nodes run via the
    doc-phase 141 LangGraph wiring; the 8 still-skeleton nodes
    are not in the wired graph yet.

    Returns a GenerateReportOutput with the partial-state counts
    (planned_sections_count, section_drafts_count, evidence_items_count)
    plus failure_reason if any node short-circuited.
    """
    risk_tier = REPORT_RISK_TIERS[input.report_type]
    report_id = uuid4()

    log.info(
        "generate_report.task_started report_id=%s report_type=%s tier=%s "
        "workspace=%s requested_by=%s",
        report_id, input.report_type, risk_tier,
        input.workspace_id, input.requested_by_user_id,
    )

    # Doc-phase 156 — parse the window iso strings into datetimes so
    # the what_changed integration can use them as DB params.
    window_start = (
        datetime.fromisoformat(input.report_window_start_iso)
        if input.report_window_start_iso
        else None
    )
    window_end = (
        datetime.fromisoformat(input.report_window_end_iso)
        if input.report_window_end_iso
        else None
    )

    initial_state = ReportBuilderState(
        report_id=report_id,
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        report_type=input.report_type,
        risk_tier=risk_tier,
        requested_by_user_id=input.requested_by_user_id,
        report_window_start=window_start,
        report_window_end=window_end,
        started_at=datetime.now(UTC),
    )

    graph = build_report_builder_graph()
    raw = await graph.ainvoke(initial_state)
    final = ReportBuilderState.model_validate(raw)

    evidence_items = sum(
        len(c.evidence)
        for d in final.section_drafts
        for c in d.claims
    )

    success = final.failure_reason is None
    failure_stage: str | None = None
    if not success:
        # Best-effort failure_stage inference from the failure_reason text.
        for marker, stage in [
            ("select_report_type", "select_report_type"),
            ("plan_sections", "plan_sections"),
            ("gather_evidence", "gather_evidence"),
            ("verify_evidence_budget", "verify_evidence_budget"),
        ]:
            if marker in (final.failure_reason or ""):
                failure_stage = stage
                break

    log.info(
        "generate_report.task_completed report_id=%s success=%s "
        "sections_planned=%d section_drafts=%d evidence_items=%d "
        "failure_stage=%s",
        report_id, success,
        len(final.sections_plan), len(final.section_drafts),
        evidence_items, failure_stage,
    )

    # Phase 2 admin surface push — list-level reports refresh + workflow_runs.
    # The per-build cockpit (admin.reports.{build_id}) already gets per-section
    # progress events from post_report_build_progress during the run; this
    # broadcast is the "new build appeared / build finished" signal for the
    # Admin/ReportBuilder index page. Best-effort.
    try:
        from app.services.laravel_bridge import post_admin_surface_updated
        admin_payload = {
            "workflow_kind": "generate_report",
            "report_id": str(report_id),
            "status": "success" if success else "failure",
            "failure_stage": failure_stage,
            "failure_reason": final.failure_reason if not success else None,
            "sections_planned": len(final.sections_plan),
            "section_drafts": len(final.section_drafts),
        }
        await post_admin_surface_updated(
            surface="workflow-runs",
            affected_props=["workflow_runs"],
            payload=admin_payload,
        )
        await post_admin_surface_updated(
            surface="reports",
            affected_props=["builds"],
            payload=admin_payload,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "generate_report: admin surface broadcasts failed report_id=%s err=%s",
            report_id, exc,
        )

    return GenerateReportOutput(
        report_id=report_id,
        success=success,
        # The 8 still-skeleton nodes haven't run, so the URI fields
        # stay None. When they graduate the wiring populates them.
        sign_off_required=risk_tier in ("R4", "R5"),
        sign_off_complete=final.sign_off_complete,
        delivery_dispatched=final.delivery_dispatched,
        failure_stage=failure_stage,
        failure_reason=final.failure_reason,
        planned_sections_count=len(final.sections_plan),
        section_drafts_count=len(final.section_drafts),
        evidence_items_count=evidence_items,
    )
