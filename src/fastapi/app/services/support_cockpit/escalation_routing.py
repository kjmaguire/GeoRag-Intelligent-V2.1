"""§25.4 escalation_routing agent — doc-phase 144.

Fifth of the 5 §25.4 support agents — closes the §25.4 suite
(ticket_triage, root_cause_investigation, support_packet,
customer_response_drafting, **escalation_routing**).

Reads a ticket's state (severity, category, age, prior actions) and
makes an escalation routing decision:

  - 'auto_resolve'        — low-severity ticket with a clean draft response;
                            ready for a human to send + close
  - 'on_call_engineer'    — critical/high severity, fresh; page on-call
  - 'sme_review'          — wrong_answer or report tickets that need
                            domain expertise before responding
  - 'queue_for_engineer'  — medium severity; standard engineer queue
  - 'wait_for_more_signal' — under-evidenced (no investigation traces) +
                              not severe; loop back for more signal

Routing decisions land on the ticket via `assigned_to_user_id`
(when a target user is set) and as an audit anchor with the full
decision rationale.

What's live in this graduation:

  - `route_escalation()` — async function that:
      1. Reads ticket + audit chain summary (triage, investigation,
         response drafted)
      2. Computes a routing decision via `_synthetic_router()`
      3. Optionally sets `assigned_to_user_id` (when supplied)
      4. Emits `support.ticket.escalation_routed` audit anchor with
         routing decision + reasoning

The router is a deterministic decision tree (synthetic stub). Real
LLM-driven routing replaces `_synthetic_router()` without touching
the surrounding orchestration.
"""

from __future__ import annotations

import logging
import os
from typing import Literal, NamedTuple
from uuid import UUID

import asyncpg

from app.audit import emit_audit

log = logging.getLogger("georag.support_cockpit.escalation_routing")


RoutingDecision = Literal[
    "auto_resolve",
    "on_call_engineer",
    "sme_review",
    "queue_for_engineer",
    "wait_for_more_signal",
]


class EscalationOutcome(NamedTuple):
    """Result of one routing call."""

    ticket_id: UUID
    decision: RoutingDecision
    rationale: str
    assigned_to_user_id: int | None
    severity: str
    category: str
    has_triage: bool
    has_investigation: bool
    has_response_draft: bool
    routing_method: str  # 'synthetic_stub' | 'llm' | ...


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _synthetic_router(
    *,
    severity: str,
    category: str,
    has_triage: bool,
    has_investigation: bool,
    has_response_draft: bool,
) -> tuple[RoutingDecision, str]:
    """Deterministic decision tree. Returns (decision, rationale).

    Decision rules (first match wins):

      1. critical → on_call_engineer
      2. wrong_answer + has_investigation → sme_review
      3. wrong_answer (no investigation) → wait_for_more_signal
      4. failed_report + has_response_draft → sme_review
      5. high severity → queue_for_engineer
      6. low severity + has_response_draft + has_investigation → auto_resolve
      7. fallback (medium severity, etc.) → queue_for_engineer
    """
    if severity == "critical":
        return ("on_call_engineer",
                "Critical-severity ticket — page on-call immediately.")

    if category == "wrong_answer":
        if has_investigation:
            return ("sme_review",
                    "Wrong-answer ticket with investigation done — needs SME "
                    "domain review of the citation chain before responding.")
        return ("wait_for_more_signal",
                "Wrong-answer ticket but investigation hasn't run yet — loop "
                "back through investigation before routing.")

    if category == "failed_report" and has_response_draft:
        return ("sme_review",
                "Failed-report ticket with draft response — SME should verify "
                "the response language matches §29.2 export compliance before send.")

    if severity == "high":
        return ("queue_for_engineer",
                "High-severity but not critical — standard engineer queue, "
                "next-business-day target.")

    if severity == "low" and has_response_draft and has_investigation:
        return ("auto_resolve",
                "Low-severity with full triage + investigation + draft "
                "response chain — ready for human review-and-send.")

    return ("queue_for_engineer",
            f"Default route: {severity}-severity {category} ticket joins "
            "standard engineer queue.")


async def route_escalation(
    *,
    ticket_id: UUID | str,
    actor_user_id: int,
    assign_to_user_id: int | None = None,
    pool: asyncpg.Pool | None = None,
) -> EscalationOutcome:
    """Make an escalation routing decision for the ticket.

    Args:
        ticket_id: UUID of the row in ops.support_tickets.
        actor_user_id: public.users.id of the routing actor.
        assign_to_user_id: optional explicit assignee. If provided,
            the agent writes it to `assigned_to_user_id`. If None,
            the row's existing assignment (if any) is preserved.
        pool: optional asyncpg pool to reuse.

    Returns:
        EscalationOutcome with the routing decision + rationale.

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
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Block-3 RLS — see customer_response_drafting for the
                # default-workspace-then-realign pattern.
                await conn.execute(
                    "SELECT set_config('app.workspace_id', $1, false)",
                    "a0000000-0000-0000-0000-000000000001",
                )
                # 1. Load ticket.
                ticket = await conn.fetchrow(
                    """
                    SELECT ticket_id::text AS ticket_id,
                           workspace_id::text AS workspace_id,
                           severity, category, status,
                           assigned_to_user_id, customer_visible_response
                      FROM ops.support_tickets
                     WHERE ticket_id = $1::uuid
                       FOR UPDATE
                    """,
                    ticket_str,
                )
                if ticket is None:
                    raise ValueError(f"ticket not found: {ticket_str}")
                await conn.execute(
                    "SELECT set_config('app.workspace_id', $1, false)",
                    ticket["workspace_id"],
                )

                # 2. Detect prior chain state from audit ledger.
                triage_count = await conn.fetchval(
                    """
                    SELECT count(*) FROM audit.audit_ledger
                     WHERE action_type = 'support.ticket.triaged'
                       AND target_id = $1
                    """,
                    ticket_str,
                )
                investigation_count = await conn.fetchval(
                    """
                    SELECT count(*) FROM audit.audit_ledger
                     WHERE action_type = 'support.ticket.investigated'
                       AND target_id = $1
                    """,
                    ticket_str,
                )

                has_triage = (triage_count or 0) > 0
                has_investigation = (investigation_count or 0) > 0
                has_response_draft = ticket["customer_visible_response"] is not None

                # 3. Route.
                decision, rationale = _synthetic_router(
                    severity=ticket["severity"],
                    category=ticket["category"],
                    has_triage=has_triage,
                    has_investigation=has_investigation,
                    has_response_draft=has_response_draft,
                )

                # 4. Optionally write assignment.
                if assign_to_user_id is not None:
                    await conn.execute(
                        """
                        UPDATE ops.support_tickets
                           SET assigned_to_user_id = $1
                         WHERE ticket_id = $2::uuid
                        """,
                        assign_to_user_id, ticket_str,
                    )

                final_assignee = (
                    assign_to_user_id
                    if assign_to_user_id is not None
                    else ticket["assigned_to_user_id"]
                )

                # 5. Audit anchor.
                await emit_audit(
                    conn,
                    action_type="support.ticket.escalation_routed",
                    workspace_id=ticket["workspace_id"],
                    actor_id=actor_user_id,
                    actor_kind="agent",
                    target_schema="ops",
                    target_table="support_tickets",
                    target_id=ticket_str,
                    payload={
                        "evaluator": "synthetic_stub",
                        "doc_phase": 144,
                        "decision": decision,
                        "rationale": rationale,
                        "severity": ticket["severity"],
                        "category": ticket["category"],
                        "has_triage": has_triage,
                        "has_investigation": has_investigation,
                        "has_response_draft": has_response_draft,
                        "assigned_to_user_id": final_assignee,
                    },
                )

                log.info(
                    "escalation_routing.completed ticket=%s decision=%s "
                    "severity=%s category=%s assignee=%s",
                    ticket_str, decision, ticket["severity"],
                    ticket["category"], final_assignee,
                )

                return EscalationOutcome(
                    ticket_id=UUID(ticket_str),
                    decision=decision,
                    rationale=rationale,
                    assigned_to_user_id=final_assignee,
                    severity=ticket["severity"],
                    category=ticket["category"],
                    has_triage=has_triage,
                    has_investigation=has_investigation,
                    has_response_draft=has_response_draft,
                    routing_method="synthetic_stub",
                )
    finally:
        if owns_pool and pool is not None:
            await pool.close()


__all__ = [
    "EscalationOutcome",
    "RoutingDecision",
    "route_escalation",
]
