"""Phase 0 ops endpoints — on-demand triggers for the LLM-calling agents.

Two routes for Phase 0 step 6:

  POST /api/v1/incidents/diagnose
      Body: {alert_label, window_minutes?, workspace_id?, trace_id?}
      → invokes LLM Incident Diagnosis Agent and returns the structured
        diagnosis. Refusals (insufficient context, schema-validation
        failure on LLM output) return HTTP 200 with outcome='refusal'
        rather than 5xx — they are well-formed responses.

  POST /api/v1/support/packets/assemble
      Body: {workspace_id, incident_id, trace_id?, incident_time?}
      → invokes Support Packet Agent, which bundles the artefacts to a
        SeaweedFS tar.gz and returns the {packet_id, storage_uri,
        bundle_bytes, ...} receipt.

Auth: same X-Service-Key + Bearer JWT pattern as the rest of FastAPI's
domain endpoints. Phase 4 will add operator-role enforcement; Phase 0
trusts the service key.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents import AgentContext
from app.agents.phase0 import (
    llm_incident_diagnosis_run,
    support_packet_assemble,
)
from app.services.auth import UserContext, extract_user_context, verify_service_key


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1",
    tags=["phase0-ops"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# /api/v1/incidents/diagnose
# ---------------------------------------------------------------------------


class IncidentDiagnoseRequest(BaseModel):
    alert_label: str = Field(..., min_length=1, max_length=200)
    window_minutes: int = Field(default=60, ge=1, le=24 * 60)
    workspace_id: UUID | None = None
    trace_id: str | None = None


class IncidentDiagnoseResponse(BaseModel):
    outcome: str
    duration_ms: int
    invocation_id: str
    diagnosis: dict[str, Any] | None = None
    error: str | None = None


@router.post("/incidents/diagnose", response_model=IncidentDiagnoseResponse)
async def diagnose_incident(
    body: IncidentDiagnoseRequest,
    user: UserContext = Depends(extract_user_context),
) -> IncidentDiagnoseResponse:
    workspace_id = body.workspace_id or (
        UUID(user.workspace_id) if user.workspace_id else None
    )
    ctx = AgentContext(
        workspace_id=workspace_id,
        trace_id=body.trace_id,
        actor_kind="user" if user.user_id else "system",
        actor_id=int(user.user_id) if user.user_id and user.user_id.isdigit() else None,
    )
    result = await llm_incident_diagnosis_run(
        ctx=ctx,
        alert_label=body.alert_label,
        window_minutes=body.window_minutes,
    )
    return IncidentDiagnoseResponse(
        outcome=result.outcome,
        duration_ms=result.duration_ms,
        invocation_id=str(result.ctx.invocation_id),
        diagnosis=result.value if isinstance(result.value, dict) else None,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# /api/v1/support/packets/assemble
# ---------------------------------------------------------------------------


class SupportPacketRequest(BaseModel):
    workspace_id: UUID
    incident_id: str = Field(..., min_length=1, max_length=200)
    trace_id: str | None = None
    incident_time: datetime | None = None


class SupportPacketResponse(BaseModel):
    outcome: str
    duration_ms: int
    invocation_id: str
    packet: dict[str, Any] | None = None
    error: str | None = None


@router.post("/support/packets/assemble", response_model=SupportPacketResponse)
async def assemble_support_packet(
    body: SupportPacketRequest,
    user: UserContext = Depends(extract_user_context),
) -> SupportPacketResponse:
    requested_by: int | None = None
    if user.user_id and user.user_id.isdigit():
        requested_by = int(user.user_id)

    ctx = AgentContext(
        workspace_id=body.workspace_id,
        # R2 idempotency — incident_id maps to document_id so two assembly
        # requests for the same incident return the cached receipt.
        document_id=body.incident_id,
        trace_id=body.trace_id,
        actor_kind="user" if user.user_id else "system",
        actor_id=requested_by,
    )
    result = await support_packet_assemble(
        ctx=ctx,
        incident_id=body.incident_id,
        trace_id=body.trace_id,
        incident_time=body.incident_time,
        requested_by=requested_by,
    )
    if result.outcome == "failure":
        # Surface internal failures as 500 so the caller doesn't think it
        # got a usable bundle. Refusals + dedupes return 200.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.error or "support_packet_assembly_failed",
        )
    return SupportPacketResponse(
        outcome=result.outcome,
        duration_ms=result.duration_ms,
        invocation_id=str(result.ctx.invocation_id),
        packet=result.value if isinstance(result.value, dict) else None,
        error=result.error,
    )
