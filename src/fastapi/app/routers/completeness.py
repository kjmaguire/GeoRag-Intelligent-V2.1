"""CC-03 Item 2 — Completeness audit endpoint."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.services.auth import UserContext, extract_user_context, verify_service_key
from app.services.completeness_audit import CompletenessAudit, CompletenessFinding

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/completeness_audit",
    tags=["completeness_audit"],
    dependencies=[Depends(verify_service_key)],
)


class FindingOut(BaseModel):
    finding_kind: str
    severity: str
    description: str
    source_page: int | None = None
    evidence: dict = Field(default_factory=dict)

    @classmethod
    def from_dataclass(cls, f: CompletenessFinding) -> "FindingOut":
        return cls(
            finding_kind=f.finding_kind,
            severity=f.severity,
            description=f.description,
            source_page=f.source_page,
            evidence=f.evidence,
        )


class AuditRunResponse(BaseModel):
    finding_run_id: uuid.UUID
    pdf_id: str
    workspace_id: uuid.UUID
    project_id: uuid.UUID | None
    findings: list[FindingOut]


def _resolve_workspace_id(user: UserContext) -> uuid.UUID:
    if not user.workspace_id:
        raise HTTPException(401, detail="workspace_id_missing_on_jwt")
    try:
        return uuid.UUID(user.workspace_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(401, detail="workspace_id_malformed") from exc


def _validate_pdf_id(pdf_id: str) -> None:
    if len(pdf_id) != 64 or any(c not in "0123456789abcdef" for c in pdf_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pdf_id_must_be_sha256_hex",
        )


@router.post(
    "/{pdf_id}",
    response_model=AuditRunResponse,
    status_code=status.HTTP_200_OK,
)
async def run_completeness_audit(
    request: Request,
    pdf_id: str,
    project_id: uuid.UUID | None = None,
    user: UserContext = Depends(extract_user_context),
) -> AuditRunResponse:
    """Run the completeness audit for a PDF; persists findings.

    Each call gets a fresh finding_run_id. Prior runs are kept in
    silver.completeness_findings (history) — UI can filter to the latest
    finding_run_id per (workspace_id, pdf_id).

    Responses
    ---------
    200  application/json — AuditRunResponse with all findings
    401  missing service key / JWT / workspace_id
    422  pdf_id_must_be_sha256_hex
    503  pg pool / service not initialised
    """
    _validate_pdf_id(pdf_id)
    workspace_id = _resolve_workspace_id(user)

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, detail="pg_pool_not_ready")

    audit = CompletenessAudit(pool=pool)
    finding_run_id, findings = await audit.run(
        workspace_id=workspace_id,
        pdf_id=pdf_id,
        project_id=project_id,
    )

    return AuditRunResponse(
        finding_run_id=finding_run_id,
        pdf_id=pdf_id,
        workspace_id=workspace_id,
        project_id=project_id,
        findings=[FindingOut.from_dataclass(f) for f in findings],
    )


@router.get(
    "/{pdf_id}/latest",
    response_model=AuditRunResponse,
)
async def get_latest_audit(
    request: Request,
    pdf_id: str,
    user: UserContext = Depends(extract_user_context),
) -> AuditRunResponse:
    """Return the latest audit-run findings for a PDF (no regenerate).

    404 when no audit has been run yet — POST to trigger one.
    """
    _validate_pdf_id(pdf_id)
    workspace_id = _resolve_workspace_id(user)

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, detail="pg_pool_not_ready")

    async with pool.acquire() as conn:
        latest_run = await conn.fetchval(
            "SELECT finding_run_id FROM silver.completeness_findings"
            " WHERE workspace_id = $1 AND pdf_id = $2"
            " ORDER BY created_at DESC LIMIT 1",
            workspace_id, pdf_id,
        )
        if latest_run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no_audit_runs_for_this_pdf",
            )
        rows = await conn.fetch(
            "SELECT finding_kind, severity, description, source_page, evidence,"
            "       project_id"
            "  FROM silver.completeness_findings"
            " WHERE finding_run_id = $1"
            " ORDER BY severity DESC, source_page NULLS LAST, created_at",
            latest_run,
        )

    findings = [
        FindingOut(
            finding_kind=r["finding_kind"],
            severity=r["severity"],
            description=r["description"],
            source_page=r["source_page"],
            evidence=r["evidence"] if isinstance(r["evidence"], dict) else {},
        )
        for r in rows
    ]
    project_id = rows[0]["project_id"] if rows else None

    return AuditRunResponse(
        finding_run_id=latest_run,
        pdf_id=pdf_id,
        workspace_id=workspace_id,
        project_id=project_id,
        findings=findings,
    )
