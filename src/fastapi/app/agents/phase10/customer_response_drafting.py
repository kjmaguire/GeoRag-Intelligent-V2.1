"""Customer Response Drafting Agent (§10.9 / §25.4).

When a ticket resolves, drafts a customer-facing response from the
resolution_summary + ticket context. Phase G.5 MVP: deterministic
template-driven draft per ticket category. The DRAFT goes back to
ops for review before sending — this agent never auto-sends.
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import asyncpg

from app.agents import AgentContext, georag_agent


_RESPONSE_TEMPLATES: dict[str, str] = {
    "wrong_answer": (
        "Thank you for flagging this issue with the answer quality. "
        "Our team has reviewed the query that produced the unexpected "
        "result and {resolution}. We have updated the relevant retrieval "
        "rules so similar queries return more accurate results going "
        "forward. Please reach out again if you see further issues."
    ),
    "failed_ingestion": (
        "Thank you for the ingestion-failure report. We investigated the "
        "affected document(s) and {resolution}. The ingestion path has "
        "been updated and the data has been re-validated. Apologies for "
        "the inconvenience."
    ),
    "failed_report": (
        "Thanks for letting us know the report generation failed. "
        "{resolution} The report has been re-built and the underlying "
        "issue addressed for future runs."
    ),
    "performance": (
        "Thank you for the performance report. The team identified "
        "the bottleneck — {resolution}. Average response time has "
        "improved. Please let us know if you observe further slowness."
    ),
    "integration_issue": (
        "Thanks for the integration report. We diagnosed the issue "
        "with the connector — {resolution}. The integration is now "
        "stable; please re-run any failed syncs at your convenience."
    ),
    "other": (
        "Thank you for reaching out. {resolution} Please let us know "
        "if there's anything else we can help with."
    ),
}


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@georag_agent(
    name="Customer Response Drafting Agent",
    risk_tier="R1",
    version="0.2.0",
)
async def customer_response_drafting(
    ctx: AgentContext,
    *,
    ticket_id: UUID | str,
    resolution_summary: str,
) -> dict[str, Any]:
    """Draft a customer-facing response for a resolved ticket.

    Phase G.5 MVP — deterministic template per category. Future LLM
    pass will rewrite for tone + workspace-specific style.

    Returns:
        {
            "ticket_id": "<uuid>",
            "draft_response": "<text>",
            "category_used": "<str>",
            "tone": "professional_friendly",
            "ready_to_send": False,   # always False — ops must review
        }
    """
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Block-3 RLS — ops.support_tickets is workspace_id-scoped.
        ws = str(ctx.workspace_id) if ctx and ctx.workspace_id \
             else "a0000000-0000-0000-0000-000000000001"
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", ws,
        )
        ticket = await conn.fetchrow(
            """
            SELECT ticket_id::text AS id, category, status
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

    category = ticket["category"] or "other"
    template = _RESPONSE_TEMPLATES.get(category, _RESPONSE_TEMPLATES["other"])
    draft = template.format(resolution=resolution_summary.strip().rstrip("."))

    return {
        "ticket_id": ticket["id"],
        "draft_response": draft,
        "category_used": category,
        "tone": "professional_friendly",
        "ready_to_send": False,
        "note": "Always review before sending; the draft is template-driven.",
    }
