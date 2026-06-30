"""§7 Report Builder Cockpit endpoints (Phase H4 UI work).

Backs the `/admin/reports` Inertia surface. Provides:

  GET  /api/v1/admin/reports/types
       Returns the 11 §15.2 report types + their canonical section
       structure (via report_planner agent).

  POST /api/v1/admin/reports/build
       Kicks off a report build for one project. Returns the
       build envelope: sections plan + section_count + a
       synthetic build_id the cockpit can poll.

  GET  /api/v1/admin/reports/builds
       Lists recent builds (operator-mode read).

  GET  /api/v1/admin/reports/builds/{build_id}
       Returns the build's progress + section status.

Builds are recorded as audit_ledger rows so they're discoverable
without a dedicated table (the §15 generate_report Hatchet workflow
writes the deliverable; this endpoint surfaces the planning + status
metadata for the cockpit).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.phase7.report_planner import report_planner
from app.services.auth import verify_service_key
from app.services.report_builder.state import ReportType

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1/admin/reports",
    tags=["report-builder-cockpit"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SectionPlanItem(BaseModel):
    section_id: str
    title: str
    template_slug: str
    required_evidence_kinds: list[str]
    map_kinds: list[str]
    chart_kinds: list[str]


class ReportTypePlan(BaseModel):
    report_type: str
    sections: list[SectionPlanItem]
    summary: str


class ReportTypeManifest(BaseModel):
    report_types: list[str]
    plans: dict[str, ReportTypePlan]


class BuildRequest(BaseModel):
    report_type: ReportType
    workspace_id: UUID
    project_id: UUID
    requested_by_user_id: int


class SectionDraft(BaseModel):
    section_id: str
    body_markdown: str
    updated_at: datetime | None = None
    updated_by_user_id: int | None = None


class BuildEnvelope(BaseModel):
    build_id: str
    report_type: str
    workspace_id: str
    project_id: str
    requested_at: datetime
    sections_planned: int
    sections: list[SectionPlanItem]
    drafts: dict[str, SectionDraft] = Field(default_factory=dict)
    status: Literal["planned", "in_flight", "completed", "failed"] = "planned"


class SectionDraftPut(BaseModel):
    body_markdown: str = Field(..., max_length=200_000)
    updated_by_user_id: int


class BuildSummary(BaseModel):
    build_id: str
    workspace_id: str
    project_id: str
    report_type: str
    requested_by_user_id: int | None = None
    requested_at: datetime
    status: str = "planned"
    sections_planned: int = 0


class BuildList(BaseModel):
    builds: list[BuildSummary]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _plan_for_type(report_type: str) -> ReportTypePlan:
    inner = getattr(report_planner, "__wrapped__", report_planner)
    out = await inner(
        ctx=None,
        workspace_id="00000000-0000-0000-0000-000000000000",  # informational
        project_id="00000000-0000-0000-0000-000000000000",
        report_type=report_type,  # type: ignore[arg-type]
    )
    return ReportTypePlan(
        report_type=out["report_type"],
        sections=[SectionPlanItem(**s) for s in out["sections"]],
        summary=out["summary"],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


_ALL_TYPES: list[str] = [
    "weekly_project_digest",
    "ingestion_quality",
    "technical_due_diligence",
    "executive_project_intelligence",
    "gis_arcgis_sync",
    "target_recommendation",
    "public_geo_overlay",
    "data_room_package",
    "what_changed",
    "ni43101_section_pack",
    "csa11348_disclosure_pack",
]


@router.get("/types", response_model=ReportTypeManifest)
async def list_report_types() -> ReportTypeManifest:
    """Return all 11 §15.2 report types + their canonical section plans."""
    plans: dict[str, ReportTypePlan] = {}
    for rt in _ALL_TYPES:
        try:
            plans[rt] = await _plan_for_type(rt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("report_types: %s plan failed err=%s", rt, exc)
    return ReportTypeManifest(report_types=_ALL_TYPES, plans=plans)


@router.post("/build", response_model=BuildEnvelope, status_code=status.HTTP_201_CREATED)
async def build_report(req: BuildRequest) -> BuildEnvelope:
    """Plan a new report build + emit the planning event.

    Phase H4 — the report planner runs synchronously; downstream
    section drafting/curation/validation happens in the §15
    generate_report Hatchet workflow. This endpoint returns the
    planning envelope + a synthetic build_id so the cockpit has
    something to poll.
    """
    plan = await _plan_for_type(req.report_type)
    build_id = uuid4()

    # Emit audit anchor so the build is discoverable + lists work.
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is not None:
        try:
            from app.audit import emit_audit
            async with pool.acquire() as conn:
                await emit_audit(
                    conn,
                    action_type="report.build.planned",
                    workspace_id=str(req.workspace_id),
                    actor_id=req.requested_by_user_id,
                    actor_kind="user",
                    target_schema="report",
                    target_table="builds",
                    target_id=str(build_id),
                    payload={
                        "build_id":          str(build_id),
                        "report_type":       req.report_type,
                        "project_id":        str(req.project_id),
                        "sections_planned":  len(plan.sections),
                        "section_ids":       [s.section_id for s in plan.sections],
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("build_report: audit emit failed err=%s", exc)

    return BuildEnvelope(
        build_id=str(build_id),
        report_type=req.report_type,
        workspace_id=str(req.workspace_id),
        project_id=str(req.project_id),
        requested_at=datetime.now(UTC),
        sections_planned=len(plan.sections),
        sections=plan.sections,
        status="planned",
    )


@router.get("/builds", response_model=BuildList)
async def list_builds(limit: int = 50) -> BuildList:
    """List recent report builds from the audit ledger."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT target_id::text         AS build_id,
                   workspace_id::text      AS workspace_id,
                   actor_id                AS requested_by_user_id,
                   created_at              AS requested_at,
                   payload                 AS payload
              FROM audit.audit_ledger
             WHERE action_type = 'report.build.planned'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )

    builds: list[BuildSummary] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        builds.append(BuildSummary(
            build_id=r["build_id"] or "",
            workspace_id=r["workspace_id"] or "",
            project_id=str(payload.get("project_id", "")),
            report_type=str(payload.get("report_type", "")),
            requested_by_user_id=r["requested_by_user_id"],
            requested_at=r["requested_at"],
            status="planned",
            sections_planned=int(payload.get("sections_planned", 0) or 0),
        ))
    return BuildList(builds=builds, total=len(builds))


class ExportRequest(BaseModel):
    workspace_id: UUID
    project_id: UUID
    report_type: ReportType
    requested_by_user_id: int
    report_window_start_iso: str | None = None
    report_window_end_iso: str | None = None
    delivery_targets: list[str] = Field(default_factory=list)


@router.post("/export", status_code=status.HTTP_201_CREATED)
async def trigger_export(req: ExportRequest) -> dict[str, Any]:
    """Trigger the §15 generate_report Hatchet workflow.

    Returns the workflow output envelope (report_id + uri set + sign-off
    state). For very large reports this can take minutes; Hatchet runs
    the workflow async — this endpoint awaits completion inline.
    """
    from app.hatchet_workflows.generate_report import (
        GenerateReportInput,
    )
    from app.hatchet_workflows.generate_report import (
        execute as generate_report_execute,
    )
    from app.services.laravel_bridge import post_report_build_progress

    export_request_id = uuid4()
    # Broadcast progress to Laravel/Reverb — the build_id used by the
    # cockpit is the export_request_id when a render is triggered fresh.
    await post_report_build_progress(
        str(export_request_id), "planning",
        message=f"generate_report started for {req.report_type}",
    )
    inp = GenerateReportInput(
        workspace_id=req.workspace_id,
        project_id=req.project_id,
        report_type=req.report_type,
        requested_by_user_id=req.requested_by_user_id,
        export_request_id=export_request_id,
        delivery_targets=req.delivery_targets,
        report_window_start_iso=req.report_window_start_iso,
        report_window_end_iso=req.report_window_end_iso,
    )
    try:
        out = await generate_report_execute.aio_mock_run(inp)
    except Exception as exc:  # noqa: BLE001
        await post_report_build_progress(
            str(export_request_id), "failed", message=str(exc),
        )
        raise
    await post_report_build_progress(
        str(export_request_id), "export.done",
        message="generate_report completed",
    )
    return out.model_dump(mode="json")


@router.get("/builds/{build_id}", response_model=BuildEnvelope)
async def get_build(build_id: UUID) -> BuildEnvelope:
    """Look up a build by id (from the audit ledger)."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT workspace_id::text  AS workspace_id,
                   created_at           AS requested_at,
                   payload              AS payload
              FROM audit.audit_ledger
             WHERE action_type = 'report.build.planned'
               AND target_id = $1::text
             LIMIT 1
            """,
            str(build_id),
        )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"build_id={build_id} not found",
        )

    payload = row["payload"] or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = {}

    report_type = str(payload.get("report_type", ""))
    plan = await _plan_for_type(report_type) if report_type else None
    sections = plan.sections if plan else []

    # Pull latest section drafts (audit-anchored) for this build.
    drafts: dict[str, SectionDraft] = {}
    async with pool.acquire() as conn:
        draft_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (payload->>'section_id')
                   payload->>'section_id'      AS section_id,
                   payload->>'body_markdown'   AS body_markdown,
                   created_at                  AS updated_at,
                   actor_id                    AS updated_by_user_id
              FROM audit.audit_ledger
             WHERE action_type = 'report.build.section.drafted'
               AND target_id   = $1::text
             ORDER BY payload->>'section_id', created_at DESC
            """,
            str(build_id),
        )
    for r in draft_rows:
        sid = r["section_id"]
        if not sid:
            continue
        drafts[sid] = SectionDraft(
            section_id=sid,
            body_markdown=r["body_markdown"] or "",
            updated_at=r["updated_at"],
            updated_by_user_id=r["updated_by_user_id"],
        )

    return BuildEnvelope(
        build_id=str(build_id),
        report_type=report_type,
        workspace_id=row["workspace_id"] or "",
        project_id=str(payload.get("project_id", "")),
        requested_at=row["requested_at"],
        sections_planned=len(sections),
        sections=sections,
        drafts=drafts,
        status="planned",
    )


@router.put(
    "/builds/{build_id}/sections/{section_id}",
    response_model=SectionDraft,
    status_code=status.HTTP_200_OK,
)
async def put_section_draft(
    build_id: UUID, section_id: str, req: SectionDraftPut,
) -> SectionDraft:
    """Save (or overwrite) the operator-edited body markdown for a section.

    Stored as a `report.build.section.drafted` audit row keyed by
    build_id (target_id) + section_id (payload). Subsequent GETs of
    the build will include the latest draft per section.
    """
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    # Resolve workspace_id from the planning audit row.
    async with pool.acquire() as conn:
        ws_row = await conn.fetchrow(
            """
            SELECT workspace_id::text AS workspace_id
              FROM audit.audit_ledger
             WHERE action_type = 'report.build.planned'
               AND target_id   = $1::text
             LIMIT 1
            """,
            str(build_id),
        )
    if ws_row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"build_id={build_id} not found",
        )

    now = datetime.now(UTC)
    from app.audit import emit_audit
    async with pool.acquire() as conn:
        await emit_audit(
            conn,
            action_type="report.build.section.drafted",
            workspace_id=ws_row["workspace_id"],
            actor_id=req.updated_by_user_id,
            actor_kind="user",
            target_schema="report",
            target_table="builds",
            target_id=str(build_id),
            payload={
                "build_id":      str(build_id),
                "section_id":    section_id,
                "body_markdown": req.body_markdown,
                "body_length":   len(req.body_markdown),
            },
        )

    return SectionDraft(
        section_id=section_id,
        body_markdown=req.body_markdown,
        updated_at=now,
        updated_by_user_id=req.updated_by_user_id,
    )


class SectionDraftHistoryEntry(BaseModel):
    audit_id: str
    body_markdown: str
    body_length: int
    updated_at: datetime
    updated_by_user_id: int | None = None


class SectionDraftHistory(BaseModel):
    build_id: str
    section_id: str
    entries: list[SectionDraftHistoryEntry]
    total: int


@router.get(
    "/builds/{build_id}/sections/{section_id}/history",
    response_model=SectionDraftHistory,
)
async def get_section_draft_history(
    build_id: UUID, section_id: str, limit: int = 50,
) -> SectionDraftHistory:
    """Return the audit-anchored revision history for a section draft,
    newest first. Each entry is a `report.build.section.drafted` audit
    row keyed on (build_id, section_id).

    The PUT path appends; this endpoint reads back the chain so
    operators can diff prior revisions and confirm the editor isn't
    silently dropping older work.
    """
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    limit = max(1, min(limit, 200))
    async with pool.acquire() as conn:
        # Verify the build exists (better error than empty + silent)
        exists = await conn.fetchval(
            """
            SELECT 1 FROM audit.audit_ledger
             WHERE action_type = 'report.build.planned'
               AND target_id   = $1::text
             LIMIT 1
            """,
            str(build_id),
        )
        if not exists:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"build_id={build_id} not found",
            )
        rows = await conn.fetch(
            """
            SELECT id::text                     AS audit_id,
                   payload->>'body_markdown'    AS body_markdown,
                   COALESCE((payload->>'body_length')::int, 0) AS body_length,
                   created_at                   AS updated_at,
                   actor_id                     AS updated_by_user_id
              FROM audit.audit_ledger
             WHERE action_type = 'report.build.section.drafted'
               AND target_id   = $1::text
               AND payload->>'section_id' = $2
             ORDER BY created_at DESC
             LIMIT $3
            """,
            str(build_id), section_id, limit,
        )

    entries = [
        SectionDraftHistoryEntry(
            audit_id=r["audit_id"],
            body_markdown=r["body_markdown"] or "",
            body_length=int(r["body_length"] or 0),
            updated_at=r["updated_at"],
            updated_by_user_id=r["updated_by_user_id"],
        )
        for r in rows
    ]
    return SectionDraftHistory(
        build_id=str(build_id),
        section_id=section_id,
        entries=entries,
        total=len(entries),
    )


__all__ = ["router"]
