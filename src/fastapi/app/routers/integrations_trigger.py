"""Phase 3 — internal route that bridges Kestra flows to Hatchet workflows.

Kestra is the integration edge: it owns external-feed scheduling,
webhook reception, and third-party connectors. When a flow needs to
do work that lives inside the GeoRAG stack — drop a row in bronze,
kick off a Hatchet workflow, write to silver — it POSTs here.

**Auth (Phase 3 Step 7 — Kestra sunset complete):**

  - **Per-flow JWT** is the only accepted auth.
    ``Authorization: Bearer <jwt>`` where the JWT carries
    ``scope=flow:<flow_name>`` for THIS flow. Each Kestra flow holds
    its own JWT in Kestra's secret store; a leak compromises one
    flow rather than every integration.
  - The legacy ``X-Service-Key`` fallback was removed at Step 7.

Registry: ``FLOW_REGISTRY`` maps a flow_name (URL path param) to the
Hatchet workflow + its input model. Phase 4+ moves this to a DB-driven
registry so operators can register flows without a code deploy.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.services.flow_jwt import verify_flow_jwt_token
from app.services.flow_registry import get_flow, list_flow_names


log = logging.getLogger("georag.integrations_trigger")

router = APIRouter(prefix="/internal/v1/integrations", tags=["integrations"])


# =============================================================================
# Flow registry — Phase 4 Step 4
# =============================================================================
# The hard-coded FLOW_REGISTRY dict was replaced with a DB-driven registry
# (workflow.flow_registry table). Adding a flow is now `INSERT INTO …`
# rather than a code deploy. The loader caches resolved entries for 60s.
#
# See app/services/flow_registry.py for the cache + resolution logic.


# =============================================================================
# Auth — per-flow JWT only (Phase 3 Step 7 — Kestra sunset)
# =============================================================================
def _check_diagnostic_auth(x_service_key: str | None = Header(default=None)) -> None:
    """Auth dep for the diagnostic listing endpoint. The shared
    X-Service-Key remains the auth here — there's no specific flow
    being triggered, so per-flow JWTs don't apply. The shared key is
    used only for read-only listing and remains scoped to internal
    operators."""
    expected = settings.FASTAPI_SERVICE_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FASTAPI_SERVICE_KEY not configured",
        )
    if x_service_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid X-Service-Key",
        )


def _check_trigger_auth(flow_name: str, authorization: str | None) -> None:
    """Per-flow trigger auth — Bearer JWT only. The legacy X-Service-Key
    fallback was removed at Phase 3 Step 7 after Kestra sunset."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization Bearer JWT",
        )
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be Bearer <jwt>",
        )
    token = authorization[7:]
    verify_flow_jwt_token(token, flow_name)


# =============================================================================
# Response models
# =============================================================================
class IntegrationTriggerResponse(BaseModel):
    flow_name: str
    workflow_run_id: str


class FlowListResponse(BaseModel):
    flows: list[str]


# =============================================================================
# Routes
# =============================================================================
@router.get(
    "/flows",
    response_model=FlowListResponse,
    dependencies=[Depends(_check_diagnostic_auth)],
)
async def list_flows() -> FlowListResponse:
    """Diagnostic — list registered flow names. Used by the Step 6
    dashboard's "what flows can be triggered" panel. Reads from the
    DB-driven registry (Phase 4 Step 4)."""
    return FlowListResponse(flows=await list_flow_names())


@router.post(
    "/{flow_name}/trigger",
    response_model=IntegrationTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_integration(
    flow_name: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> IntegrationTriggerResponse:
    """Dispatch a registered flow.

    Returns 202 Accepted with the Hatchet workflow_run_id. The caller
    (Kestra flow) does NOT wait for completion; flows that need a
    synchronous result should poll a separate status endpoint added
    in a later phase.

    Auth: per-flow Bearer JWT only. See module docstring.
    """
    _check_trigger_auth(flow_name, authorization)

    entry = await get_flow(flow_name)
    if entry is None:
        known = await list_flow_names()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown flow_name: {flow_name}. Registered: {known}",
        )

    try:
        validated = entry.input_model.model_validate(payload or {})
    except ValidationError as ve:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"flow_name": flow_name, "errors": ve.errors()},
        ) from ve

    log.info(
        "integrations.trigger flow=%s payload_keys=%s",
        flow_name,
        sorted(validated.model_dump().keys()),
    )
    ref = await entry.workflow.aio_run_no_wait(validated)
    return IntegrationTriggerResponse(
        flow_name=flow_name,
        workflow_run_id=ref.workflow_run_id,
    )
