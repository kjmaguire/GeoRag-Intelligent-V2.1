"""§25.4 customer_response_drafting agent — doc-phase 143.

Fourth of the 5 §25.4 support agents.

Reads a triaged + investigated ticket and drafts a customer-visible
response. The drafted text lands on `ops.support_tickets.customer_visible_response`.

What's live in this graduation:

  - `draft_customer_response()` — async function that:
      1. Reads the ticket + its triage/investigation/packet chain
      2. Pulls the top investigation's top_cause_summary
      3. Synthesizes a templated response (severity-aware tone)
      4. UPDATEs `customer_visible_response` on the ticket
      5. Emits a `support.ticket.response_drafted` audit anchor

The response synthesizer is a deterministic template engine
(not an LLM). Templates vary by severity (critical → apology +
escalation note; low → softer wording) and category (failed_ingestion
→ retry instructions; wrong_answer → cite-source reminder; etc.).

The drafted response is **draft-state** — the schema's
`customer_visible_response` field is "drafted, not sent." Sending
to the customer is a separate human-in-the-loop step (the agent
NEVER auto-sends).

Real LLM-based drafting replaces `_synthesize_response()` without
touching the surrounding orchestration.
"""

from __future__ import annotations
from app.db import lookup_and_rescope

import logging
import os
from typing import NamedTuple
from uuid import UUID

import asyncpg

from app.audit import emit_audit

log = logging.getLogger("georag.support_cockpit.customer_response_drafting")


# Per-category opening sentence used by the synthetic template engine.
_CATEGORY_OPENING: dict[str, str] = {
    "wrong_answer": (
        "Thanks for flagging this — getting a wrong answer is exactly the "
        "kind of thing we want to fix fast, and the source-citation trail "
        "you reported lets us trace it."
    ),
    "failed_ingestion": (
        "Sorry your upload didn't ingest cleanly. We've pulled the ingest "
        "workflow logs for the timeframe you reported and our triage shows "
        "where it broke."
    ),
    "failed_report": (
        "Apologies for the failed report export. We've pulled the workflow "
        "trace for the export attempt and the failure mode is clear from "
        "the audit chain."
    ),
    "integration_issue": (
        "Thanks for the integration report — our triage shows recent "
        "external_notification and webhook activity, and we can see where "
        "the handshake went off the rails."
    ),
    "performance": (
        "Thanks for flagging the slowness. Performance reports like this "
        "are valuable because the audit chain doesn't always capture user "
        "experience directly."
    ),
    "other": (
        "Thanks for reaching out. We've reviewed your ticket against the "
        "recent audit activity for your workspace."
    ),
}

# Per-severity tone snippet appended after the opening.
_SEVERITY_TONE: dict[str, str] = {
    "critical": (
        "Because this is rated **critical**, we're treating it as a top-of-queue "
        "incident — you'll get a status update within the hour even if the fix "
        "takes longer."
    ),
    "high": (
        "We've flagged this as **high-priority**; expect a substantive update "
        "from us within one business day."
    ),
    "medium": (
        "We're treating this as a normal-priority ticket and will follow up "
        "with our findings within two business days."
    ),
    "low": (
        "We've logged this as low-priority and will fold it into our next "
        "weekly triage pass; reply on this thread if the urgency changes on "
        "your end."
    ),
}


class DraftOutcome(NamedTuple):
    """Result of one drafting call."""

    ticket_id: UUID
    category: str
    severity: str
    response_text: str
    response_word_count: int
    drafting_method: str  # 'synthetic_stub' | 'llm' | ...


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _synthesize_response(
    *,
    category: str,
    severity: str,
    investigation_summary: str | None,
) -> str:
    """Deterministic template synthesis. Returns the drafted body text.

    Structure:
      paragraph 1 — opening (per category)
      paragraph 2 — severity tone
      paragraph 3 — investigation summary (if available)
      paragraph 4 — closing (uniform)
    """
    opening = _CATEGORY_OPENING.get(category, _CATEGORY_OPENING["other"])
    tone = _SEVERITY_TONE.get(severity, _SEVERITY_TONE["medium"])

    if investigation_summary:
        summary_para = (
            f"Initial triage notes:\n"
            f"> {investigation_summary.strip()}\n\n"
            f"We'll dig into this signal and confirm root cause before we "
            f"close the ticket."
        )
    else:
        summary_para = (
            "We're still gathering signal — once root-cause investigation "
            "completes we'll send a follow-up with the findings."
        )

    closing = (
        "If anything we've described above is off, reply on this thread "
        "with what we missed.\n\n"
        "— GeoRAG support"
    )

    return (
        f"{opening}\n\n{tone}\n\n{summary_para}\n\n{closing}\n\n"
        f"<!-- [synthetic_stub doc-phase 143] generated by template engine; "
        f"swap for LLM drafter when §25.4 prompt locks. -->"
    )


async def draft_customer_response(
    *,
    ticket_id: UUID | str,
    actor_user_id: int,
    pool: asyncpg.Pool | None = None,
) -> DraftOutcome:
    """Draft a customer-visible response for the ticket.

    Args:
        ticket_id: UUID of the row in ops.support_tickets.
        actor_user_id: public.users.id of the drafting user/agent.
        pool: optional asyncpg pool to reuse.

    Returns:
        DraftOutcome with the drafted response_text + word count.

    Raises:
        ValueError if the ticket doesn't exist.
    """
    ticket_str = str(ticket_id) if isinstance(ticket_id, UUID) else ticket_id

    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        # ADR-0014 — two-phase workspace scoping. The helper:
        #   1. Bootstraps the GUC to default tenant (cross-tenant ticket
        #      lookup) via bootstrap_workspace_id(reason=...), so the
        #      elevation is COUNTED in WORKSPACE_RESOLUTION_FAILURES
        #   2. Runs the lookup_sql
        #   3. UUID-validates ticket["workspace_id"] (catches malformed
        #      ticket rows before SET LOCAL interpolation — the real
        #      defect ADR-0014 closes)
        #   4. Rebinds the GUC to the ticket's workspace
        #   5. Yields (conn, ticket) — subsequent writes are scoped.
        # Raises BareConnectionError if the ticket doesn't exist or has
        # a malformed workspace_id; the caller catches Exception → 500.
        async with lookup_and_rescope(
            pool,
            lookup_sql="""
                SELECT ticket_id::text AS ticket_id,
                       workspace_id::text AS workspace_id,
                       category, severity, status
                  FROM ops.support_tickets
                 WHERE ticket_id = $1::uuid
                   FOR UPDATE
                """,
            lookup_args=(ticket_str,),
            site="support_cockpit.customer_response_drafting",
            bootstrap_reason="support_cockpit.elevated_lookup",
        ) as (conn, ticket):
            # 2. Look up the most-recent investigation summary, if any.
            inv_row = await conn.fetchrow(
                """
                SELECT trace_summary
                  FROM ops.support_ticket_traces
                 WHERE ticket_id = $1::uuid
                 ORDER BY added_at DESC
                 LIMIT 1
                """,
                ticket_str,
            )
            investigation_summary = (
                inv_row["trace_summary"] if inv_row else None
            )

            # 3. Synthesize.
            response_text = _synthesize_response(
                category=ticket["category"],
                severity=ticket["severity"],
                investigation_summary=investigation_summary,
            )

            # 4. Persist on the ticket.
            await conn.execute(
                """
                UPDATE ops.support_tickets
                   SET customer_visible_response = $1
                 WHERE ticket_id = $2::uuid
                """,
                response_text, ticket_str,
            )

            # 5. Audit anchor.
            word_count = len(response_text.split())
            await emit_audit(
                conn,
                action_type="support.ticket.response_drafted",
                workspace_id=ticket["workspace_id"],
                actor_id=actor_user_id,
                actor_kind="agent",
                target_schema="ops",
                target_table="support_tickets",
                target_id=ticket_str,
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": 143,
                    "category": ticket["category"],
                    "severity": ticket["severity"],
                    "response_word_count": word_count,
                    "investigation_summary_used": investigation_summary is not None,
                },
            )

            log.info(
                "customer_response_drafting.completed ticket=%s "
                "category=%s severity=%s word_count=%d",
                ticket_str, ticket["category"], ticket["severity"],
                word_count,
            )

            return DraftOutcome(
                ticket_id=UUID(ticket_str),
                category=ticket["category"],
                severity=ticket["severity"],
                response_text=response_text,
                response_word_count=word_count,
                drafting_method="synthetic_stub",
            )
    finally:
        if owns_pool and pool is not None:
            await pool.close()


__all__ = [
    "DraftOutcome",
    "draft_customer_response",
]
