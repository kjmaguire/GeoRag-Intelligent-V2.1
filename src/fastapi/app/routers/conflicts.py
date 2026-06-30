"""§7.4 Conflict Resolver review queue (Phase H4 UI work).

  GET  /api/v1/admin/conflicts/recent
       Pulls recent conflict-related entries from audit.audit_ledger
       (action_type ILIKE 'report.conflict%' or '%.conflict_resolved').

  POST /api/v1/admin/conflicts/run
       Runs the Conflict Resolver Agent against a caller-supplied
       claim ledger. Used as a test-bench from the UI.

Authentication: service-key.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.phase7.conflict_resolver import conflict_resolver
from app.services.auth import verify_service_key

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1/admin/conflicts",
    tags=["conflicts-review-queue"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ConflictAuditEntry(BaseModel):
    id: str
    workspace_id: str | None
    action_type: str
    created_at: datetime
    target_id: str | None
    payload: dict[str, Any]


class ConflictAuditList(BaseModel):
    entries: list[ConflictAuditEntry]
    total: int


class ClaimInput(BaseModel):
    claim_id: str
    text: str
    validated: bool = True
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class RunResolverRequest(BaseModel):
    workspace_id: UUID
    section_id: str = "test-bench"
    claims: list[ClaimInput] = Field(..., min_length=1)
    workspace_data_version: int | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/recent", response_model=ConflictAuditList)
async def list_recent_conflicts(limit: int = 50) -> ConflictAuditList:
    """Pull recent conflict-related audit ledger entries."""
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
            SELECT id::text          AS id,
                   workspace_id::text AS workspace_id,
                   action_type        AS action_type,
                   created_at         AS created_at,
                   target_id          AS target_id,
                   payload            AS payload
              FROM audit.audit_ledger
             WHERE action_type ILIKE '%conflict%'
                OR action_type ILIKE 'report.export.%'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )

    out: list[ConflictAuditEntry] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        out.append(ConflictAuditEntry(
            id=r["id"],
            workspace_id=r["workspace_id"],
            action_type=r["action_type"],
            created_at=r["created_at"],
            target_id=r["target_id"],
            payload=payload if isinstance(payload, dict) else {},
        ))
    return ConflictAuditList(entries=out, total=len(out))


@router.post("/run")
async def run_conflict_resolver(req: RunResolverRequest) -> dict[str, Any]:
    """Run the Conflict Resolver Agent against a caller-supplied
    claim ledger. Used as a UI test-bench."""
    inner = getattr(conflict_resolver, "__wrapped__", conflict_resolver)
    claims_payload = [
        {
            "claim_id":  c.claim_id,
            "text":      c.text,
            "validated": c.validated,
            "evidence":  c.evidence,
        }
        for c in req.claims
    ]
    result = await inner(
        ctx=None,
        workspace_id=req.workspace_id,
        section_id=req.section_id,
        claims=claims_payload,
        workspace_data_version=req.workspace_data_version,
    )
    return result


__all__ = ["router"]
