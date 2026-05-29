"""Support Cockpit agent dispatch endpoints — Phase G.5 follow-up.

Provides one POST endpoint per phase10 support agent. The Laravel
Support Cockpit (``/admin/support-cockpit``) proxies operator clicks
through these routes, which authenticate via service-key + dispatch to
the underlying phase10 agent body.

Routes mounted under ``/api/v1/admin/support``:

  POST /agents/ticket-triage             body: {ticket_id}
  POST /agents/support-packet            body: {ticket_id, include_audit_anchors?, include_recent_runs?}
  POST /agents/root-cause-investigation  body: {ticket_id, trace_ids?}
  POST /agents/customer-response-draft   body: {ticket_id, resolution_summary}
  POST /agents/escalation-routing        body: {ticket_id, apply?}

Each route returns the agent's structured output dict (200 OK) or a
4xx/5xx for client/server errors. The agents themselves return
``{"error": "..."}`` for ticket-not-found rather than raising, so the
controller can surface the failure without an HTTP error.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.context import AgentContext
from app.agents.phase10.customer_response_drafting import (
    customer_response_drafting,
)
from app.agents.phase10.escalation_routing import escalation_routing
from app.agents.phase10.root_cause_investigation import (
    root_cause_investigation,
)
from app.agents.phase10.support_packet import support_packet
from app.agents.phase10.ticket_triage import ticket_triage
from app.services.auth import verify_service_key


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1/admin/support",
    tags=["support-cockpit"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class _TicketIdRequest(BaseModel):
    ticket_id: UUID


class TicketTriageRequest(_TicketIdRequest):
    pass


class SupportPacketRequest(_TicketIdRequest):
    include_audit_anchors: int = Field(default=10, ge=1, le=50)
    include_recent_runs: int = Field(default=5, ge=1, le=20)


class RootCauseRequest(_TicketIdRequest):
    trace_ids: list[str] = Field(default_factory=list, max_length=10)


class CustomerResponseRequest(_TicketIdRequest):
    resolution_summary: str = Field(..., min_length=1, max_length=2000)


class EscalationRoutingRequest(_TicketIdRequest):
    apply: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent_ctx() -> AgentContext:
    """Build a minimal AgentContext for direct invocation.

    The Support Cockpit doesn't yet pass the operator's user_id /
    workspace_id through — the agents currently look those up themselves
    from the ticket row. Future hardening can populate these via a
    Depends() that reads the JWT.
    """
    return AgentContext(actor_kind="support_cockpit")


def _unwrap(agent_fn: Any) -> Any:
    """Return the agent's underlying coroutine, bypassing the
    ``@georag_agent`` wrapper.

    The wrapper requires the global runtime to be registered, which
    the FastAPI lifespan handles for normal startup. For these admin
    endpoints we invoke the inner function directly so test invocations
    + early-lifecycle calls (before runtime registration) still work.
    """
    return getattr(agent_fn, "__wrapped__", agent_fn)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/agents/ticket-triage")
async def run_ticket_triage(req: TicketTriageRequest) -> dict[str, Any]:
    """Suggest severity + category for a ticket."""
    try:
        return await _unwrap(ticket_triage)(_agent_ctx(), ticket_id=req.ticket_id)
    except Exception as exc:
        logger.exception("ticket_triage failed for %s", req.ticket_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"ticket_triage failed: {type(exc).__name__}: {exc}",
        ) from exc


@router.post("/agents/support-packet")
async def run_support_packet(req: SupportPacketRequest) -> dict[str, Any]:
    """Assemble diagnostic bundle: ticket + recent audit anchors + runs."""
    try:
        return await _unwrap(support_packet)(
            _agent_ctx(),
            ticket_id=req.ticket_id,
            include_audit_anchors=req.include_audit_anchors,
            include_recent_runs=req.include_recent_runs,
        )
    except Exception as exc:
        logger.exception("support_packet failed for %s", req.ticket_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"support_packet failed: {type(exc).__name__}: {exc}",
        ) from exc


@router.post("/agents/root-cause-investigation")
async def run_root_cause_investigation(req: RootCauseRequest) -> dict[str, Any]:
    """Draft a root-cause hypothesis from ticket + workflow signals."""
    try:
        return await _unwrap(root_cause_investigation)(
            _agent_ctx(),
            ticket_id=req.ticket_id,
            trace_ids=req.trace_ids,
        )
    except Exception as exc:
        logger.exception("root_cause_investigation failed for %s", req.ticket_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"root_cause_investigation failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc


@router.post("/agents/customer-response-draft")
async def run_customer_response_draft(
    req: CustomerResponseRequest,
) -> dict[str, Any]:
    """Draft a customer-facing response. Always returns ready_to_send=False."""
    try:
        return await _unwrap(customer_response_drafting)(
            _agent_ctx(),
            ticket_id=req.ticket_id,
            resolution_summary=req.resolution_summary,
        )
    except Exception as exc:
        logger.exception("customer_response_drafting failed for %s", req.ticket_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"customer_response_drafting failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc


@router.post("/agents/escalation-routing")
async def run_escalation_routing(
    req: EscalationRoutingRequest,
) -> dict[str, Any]:
    """Recommend (advisory) escalation routing per severity."""
    try:
        return await _unwrap(escalation_routing)(
            _agent_ctx(),
            ticket_id=req.ticket_id,
            apply=req.apply,
        )
    except Exception as exc:
        logger.exception("escalation_routing failed for %s", req.ticket_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"escalation_routing failed: {type(exc).__name__}: {exc}",
        ) from exc


__all__ = ["router"]
