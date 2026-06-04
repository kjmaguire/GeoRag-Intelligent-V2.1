"""Root Cause Investigation Agent (§10.9 / §25.4).

Given a ticket + optional correlated trace_ids, drafts a root-cause
hypothesis by inspecting recent workflow_runs + audit anchors for the
ticket's workspace, looking for failures or anomalies in the same
time window as the ticket. Phase G.5 MVP — no LLM call yet; the
hypothesis is a deterministic narrative built from the most recent
failure rows.
"""
from __future__ import annotations
from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID

import os
from typing import Any
from uuid import UUID

import asyncpg

from app.agents import AgentContext, georag_agent


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@georag_agent(
    name="Root Cause Investigation Agent",
    risk_tier="R1",
    version="0.2.0",
)
async def root_cause_investigation(
    ctx: AgentContext,
    *,
    ticket_id: UUID | str,
    trace_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Draft a root-cause hypothesis from ticket + workflow signals.

    Returns:
        {
            "ticket_id": "<uuid>",
            "hypothesis": "<narrative>",
            "supporting_signals": [
                {"kind": "failed_workflow", "workflow_name": "...",
                 "status": "failed", "created_at": "..."}
            ],
            "similar_recent_tickets": [<ticket_id>, …],
            "trace_ids_correlated": [<trace_id>, …],
            "confidence": "low" | "medium" | "high",
        }

    Future: replace the deterministic narrative with an LLM call that
    reads workflow logs from Langfuse. The shape doesn't change.
    """
    trace_ids = trace_ids or []
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Block-3 RLS — ops.support_tickets / support_ticket_traces are
        # workspace_id-scoped.
        ws = str(ctx.workspace_id) if ctx and ctx.workspace_id \
             else LEGACY_DEFAULT_TENANT_UUID
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", ws,
        )
        ticket = await conn.fetchrow(
            """
            SELECT ticket_id::text AS id,
                   workspace_id::text AS workspace_id,
                   category, severity, description,
                   reported_at
              FROM ops.support_tickets
             WHERE ticket_id = $1::uuid
            """,
            str(ticket_id),
        )
        if ticket is None:
            return {
                "ticket_id": str(ticket_id),
                "error": "ticket not found",
            }

        ws = ticket["workspace_id"]
        supporting: list[dict] = []
        similar_tickets: list[str] = []

        if ws:
            # Failed workflows in the 48h window prior to the ticket.
            # workflow.workflow_runs uses (run_id, workflow_kind, started_at,
            # status IN ('queued','running','success','failure','cancelled',
            # 'timed_out')). See database/raw/phase0/30-layer-c-workflow-runs.sql.
            try:
                fail_rows = await conn.fetch(
                    """
                    SELECT run_id::text AS id,
                           workflow_kind AS workflow_name, status,
                           started_at::text AS created_at
                      FROM workflow.workflow_runs
                     WHERE workspace_id = $1::uuid
                       AND status IN ('failure', 'cancelled', 'timed_out')
                       AND started_at >= $2 - INTERVAL '48 hours'
                       AND started_at <= $2 + INTERVAL '1 hour'
                     ORDER BY started_at DESC
                     LIMIT 10
                    """,
                    ws, ticket["reported_at"],
                )
                supporting = [
                    {"kind": "failed_workflow", **dict(r)}
                    for r in fail_rows
                ]
            except Exception:
                supporting = []

            # Tickets in the same workspace + same category (last 30 days).
            try:
                sim_rows = await conn.fetch(
                    """
                    SELECT ticket_id::text AS id
                      FROM ops.support_tickets
                     WHERE workspace_id = $1::uuid
                       AND category = $2
                       AND ticket_id != $3::uuid
                       AND reported_at >= NOW() - INTERVAL '30 days'
                     ORDER BY reported_at DESC
                     LIMIT 5
                    """,
                    ws, ticket["category"], str(ticket_id),
                )
                similar_tickets = [r["id"] for r in sim_rows]
            except Exception:
                similar_tickets = []
    finally:
        await conn.close()

    # Build the deterministic narrative.
    parts: list[str] = []
    if supporting:
        names = ", ".join(s["workflow_name"] for s in supporting[:3])
        parts.append(
            f"In the 48h before this ticket fired, {len(supporting)} "
            f"workflow run(s) failed in the same workspace ({names}). "
            f"That correlation is the strongest signal."
        )
        confidence = "high" if len(supporting) >= 2 else "medium"
    else:
        parts.append(
            "No correlated workflow failures found in the 48h window. "
            "The ticket may stem from a user-facing UX issue rather "
            "than an upstream pipeline failure."
        )
        confidence = "low"

    if similar_tickets:
        parts.append(
            f"{len(similar_tickets)} similar ticket(s) in the same "
            f"category were filed in the last 30 days — this is a "
            "recurring pattern, not a one-off."
        )
        if confidence == "low":
            confidence = "medium"

    if trace_ids:
        parts.append(
            f"{len(trace_ids)} trace_id(s) were provided; correlate in "
            "Langfuse for span-level detail."
        )

    return {
        "ticket_id": ticket["id"],
        "hypothesis": " ".join(parts),
        "supporting_signals": supporting,
        "similar_recent_tickets": similar_tickets,
        "trace_ids_correlated": trace_ids,
        "confidence": confidence,
    }
