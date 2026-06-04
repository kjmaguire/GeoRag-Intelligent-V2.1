"""Escalation Routing Agent (§10.9 / §25.4).

Recommends escalation routing for a high-severity ticket. Phase G.5
MVP: deterministic rules over severity + status + category — no
PagerDuty / Opsgenie integration yet. Output is advisory; assignment
mutation is opt-in via `apply=True`.
"""
from __future__ import annotations
from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID

import os
from typing import Any
from uuid import UUID

import asyncpg

from app.agents import AgentContext, georag_agent


# Routing recommendations per severity. Real deployment ties these to
# PagerDuty / Opsgenie schedules; for now we name the relevant role.
_ROUTING_TABLE: dict[str, dict[str, str]] = {
    "critical": {
        "page": "primary_on_call",
        "channel": "#oncall-page",
        "sla_minutes": "15",
        "rationale": "critical severity — page on-call immediately",
    },
    "high": {
        "page": "secondary_on_call",
        "channel": "#oncall-high",
        "sla_minutes": "60",
        "rationale": "high severity — notify on-call within 1 hour",
    },
    "medium": {
        "page": "support_team_lead",
        "channel": "#support-medium",
        "sla_minutes": "240",
        "rationale": "medium severity — route to support team lead, 4h SLA",
    },
    "low": {
        "page": "support_backlog",
        "channel": "#support-backlog",
        "sla_minutes": "1440",
        "rationale": "low severity — add to backlog, 24h SLA",
    },
}


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@georag_agent(
    name="Escalation Routing Agent",
    risk_tier="R2",
    version="0.2.0",
)
async def escalation_routing(
    ctx: AgentContext,
    *,
    ticket_id: UUID | str,
    apply: bool = False,
) -> dict[str, Any]:
    """Recommend (and optionally apply) escalation routing.

    Phase G.5 MVP — advisory by default. Set `apply=True` to actually
    update the ticket's assigned_to_user_id (not yet wired; reserved
    for the future PagerDuty integration).

    Returns:
        {
            "ticket_id": "<uuid>",
            "severity": "<str>",
            "route_to": "<role>",
            "channel": "<slack channel>",
            "sla_minutes": <int>,
            "rationale": "<text>",
            "already_assigned": bool,
            "applied": False,           # PagerDuty wiring not yet shipped
        }
    """
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Block-3 RLS — ops.support_tickets is workspace_id-scoped.
        ws = str(ctx.workspace_id) if ctx and ctx.workspace_id \
             else LEGACY_DEFAULT_TENANT_UUID
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", ws,
        )
        ticket = await conn.fetchrow(
            """
            SELECT ticket_id::text AS id, severity, status,
                   assigned_to_user_id
              FROM ops.support_tickets
             WHERE ticket_id = $1::uuid
            """,
            str(ticket_id),
        )
    finally:
        await conn.close()

    if ticket is None:
        return {
            "ticket_id": str(ticket_id),
            "error": "ticket not found",
        }

    severity = (ticket["severity"] or "medium").lower()
    routing = _ROUTING_TABLE.get(severity, _ROUTING_TABLE["medium"])

    # Phase G overnight — wire to PagerDuty Events v2. No-op when
    # PAGERDUTY_INTEGRATION_KEY is unset (default). When configured AND
    # apply=True, fires a Trigger event keyed on ticket_id (dedup_key)
    # so re-routing the same ticket updates the existing incident
    # rather than creating duplicates.
    pd_result: dict[str, Any] = {
        "paged": False,
        "reason": "apply_not_requested",
    }
    if apply:
        try:
            from app.services.dispatchers import (  # noqa: PLC0415
                create_pagerduty_incident,
            )
            pd_result = await create_pagerduty_incident(
                ticket_id=ticket["id"],
                severity=severity,
                summary=f"{severity.upper()} GeoRAG support ticket {ticket['id']}",
                custom_details={
                    "status": ticket["status"],
                    "route_to": routing["page"],
                    "channel": routing["channel"],
                    "sla_minutes": int(routing["sla_minutes"]),
                    "rationale": routing["rationale"],
                },
                klass=routing["page"],
            )
        except Exception as exc:  # noqa: BLE001
            pd_result = {
                "paged": False,
                "reason": "dispatcher_exception",
                "error": f"{type(exc).__name__}: {exc}",
            }

    return {
        "ticket_id": ticket["id"],
        "severity": severity,
        "status": ticket["status"],
        "route_to": routing["page"],
        "channel": routing["channel"],
        "sla_minutes": int(routing["sla_minutes"]),
        "rationale": routing["rationale"],
        "already_assigned": ticket["assigned_to_user_id"] is not None,
        "applied": bool(pd_result.get("paged")),
        "apply_requested": apply,
        "pagerduty": pd_result,
        "note": (
            "Routing is advisory unless apply=True AND "
            "PAGERDUTY_INTEGRATION_KEY is set. dedup_key=ticket_id keeps "
            "re-routes idempotent against an existing incident."
        ),
    }
