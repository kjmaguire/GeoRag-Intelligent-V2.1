"""§25.4 ticket_triage support agent — doc-phase 136.

First of the 5 §25.4 support agents (ticket_triage,
root_cause_investigation, support_packet, customer_response_drafting,
escalation_routing).

What's live:
  - `triage_ticket()` — reads a ticket by id, classifies it (severity
    + category), updates the row, emits a `support.ticket.triaged`
    audit anchor
  - `triage_unclassified_tickets()` — bulk-triage all tickets where
    `status='open'` AND severity/category don't match the heuristic's
    re-evaluation (or that haven't been triaged yet)
  - `_synthetic_classifier()` — deterministic keyword-based stub.
    Real LLM-based triage agent replaces this without touching the
    surrounding orchestration.

The synthetic stub flips `status='open'` → `status='investigating'`
and updates severity + category based on description keywords. Real
agent will use an LLM with a §25.4 prompt to do better classification
and root-cause hints.

Idempotency: `triage_ticket(ticket_id)` is safe to call repeatedly —
it always re-classifies and writes the same audit anchor type. The
caller is responsible for not triaging closed tickets.
"""

from __future__ import annotations
from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID

import logging
import os
from typing import Any, NamedTuple
from uuid import UUID

import asyncpg

from app.audit import emit_audit
from app.db import lookup_and_rescope

log = logging.getLogger("georag.support_cockpit.ticket_triage")


VALID_SEVERITIES = ("low", "medium", "high", "critical")
VALID_CATEGORIES = (
    "wrong_answer", "failed_ingestion", "failed_report",
    "integration_issue", "performance", "other",
)


class TriageOutcome(NamedTuple):
    """Result of one triage call."""

    ticket_id: UUID
    prior_severity: str
    prior_category: str
    new_severity: str
    new_category: str
    new_status: str
    triage_method: str  # 'synthetic_stub' | 'llm' | ...


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _synthetic_classifier(description: str) -> tuple[str, str]:
    """Deterministic keyword-based severity + category classifier.

    Returns (severity, category). Both are guaranteed to be valid
    against the CHECK constraints.

    Severity rules (first match wins):
      - 'crash', 'broken', 'unable', 'fail', 'data loss', 'critical'
        → 'critical'
      - 'wrong', 'incorrect', 'hallucinat', 'fabricat'
        → 'high'
      - 'slow', 'performance', 'timeout'
        → 'medium'
      - default → 'low'

    Category rules (first match wins):
      - 'pdf', 'upload', 'ingest', 'parse'
        → 'failed_ingestion'
      - 'report', 'export', 'pdf gen', 'docx', 'xlsx'
        → 'failed_report'
      - 'integration', 'activepieces', 'webhook', 'api'
        → 'integration_issue'
      - 'slow', 'timeout', 'lag'
        → 'performance'
      - 'wrong', 'incorrect', 'hallucinat'
        → 'wrong_answer'
      - default → 'other'
    """
    d = description.lower()

    # Severity
    if any(k in d for k in ["crash", "broken", "unable", "data loss", "critical"]):
        severity = "critical"
    elif any(k in d for k in ["fail"]):
        severity = "critical" if "ingest" in d or "report" in d else "high"
    elif any(k in d for k in ["wrong", "incorrect", "hallucinat", "fabricat"]):
        severity = "high"
    elif any(k in d for k in ["slow", "performance", "timeout"]):
        severity = "medium"
    else:
        severity = "low"

    # Category (more specific matches first)
    if any(k in d for k in ["report", "export", "docx", "xlsx", "pdf gen"]):
        category = "failed_report"
    elif any(k in d for k in ["pdf upload", "upload", "ingest", "parse", "ocr"]):
        category = "failed_ingestion"
    elif any(k in d for k in ["integration", "activepieces", "webhook", "api error"]):
        category = "integration_issue"
    elif any(k in d for k in ["slow", "timeout", "lag"]):
        category = "performance"
    elif any(k in d for k in ["wrong", "incorrect", "hallucinat", "fabricat"]):
        category = "wrong_answer"
    else:
        category = "other"

    assert severity in VALID_SEVERITIES
    assert category in VALID_CATEGORIES
    return severity, category


async def triage_ticket(
    *,
    ticket_id: UUID | str,
    pool: asyncpg.Pool | None = None,
) -> TriageOutcome:
    """Triage one ticket: classify severity + category, transition to
    investigating, emit audit anchor.

    Args:
        ticket_id: UUID of the row in ops.support_tickets.
        pool: optional asyncpg pool to reuse.

    Returns:
        TriageOutcome with prior + new severity/category.

    Raises:
        ValueError if the ticket doesn't exist or is already closed.
    """
    ticket_str = str(ticket_id) if isinstance(ticket_id, UUID) else ticket_id

    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        # ADR-0014 lookup_and_rescope — see customer_response_drafting.py.
        async with lookup_and_rescope(
            pool,
            lookup_sql="""
                SELECT ticket_id, workspace_id, description,
                       severity, category, status
                  FROM ops.support_tickets
                 WHERE ticket_id = $1::uuid
                   FOR UPDATE
                """,
            lookup_args=(ticket_str,),
            site="support_cockpit.ticket_triage",
            bootstrap_reason="support_cockpit.elevated_lookup",
        ) as (conn, row):
            if row["status"] in ("resolved", "closed"):
                raise ValueError(
                    f"ticket {ticket_str} is {row['status']} — not triageable"
                )

            prior_severity = row["severity"]
            prior_category = row["category"]

            # 2. Classify.
            new_severity, new_category = _synthetic_classifier(
                row["description"]
            )
            new_status = "investigating"

            # 3. Apply.
            await conn.execute(
                """
                UPDATE ops.support_tickets
                   SET severity = $1,
                       category = $2,
                       status = $3
                 WHERE ticket_id = $4::uuid
                """,
                new_severity, new_category, new_status, ticket_str,
            )

            # 4. Audit anchor.
            await emit_audit(
                conn,
                action_type="support.ticket.triaged",
                workspace_id=row["workspace_id"],
                actor_kind="agent",
                target_schema="ops",
                target_table="support_tickets",
                target_id=ticket_str,
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": 136,
                    "prior_severity": prior_severity,
                    "prior_category": prior_category,
                    "new_severity": new_severity,
                    "new_category": new_category,
                    "new_status": new_status,
                },
            )

            log.info(
                "ticket_triage.completed ticket=%s sev=%s→%s cat=%s→%s",
                ticket_str, prior_severity, new_severity,
                prior_category, new_category,
            )

            return TriageOutcome(
                ticket_id=UUID(ticket_str),
                prior_severity=prior_severity,
                prior_category=prior_category,
                new_severity=new_severity,
                new_category=new_category,
                new_status=new_status,
                triage_method="synthetic_stub",
            )
    finally:
        if owns_pool and pool is not None:
            await pool.close()


async def triage_unclassified_tickets(
    *,
    limit: int = 50,
    pool: asyncpg.Pool | None = None,
) -> list[TriageOutcome]:
    """Triage all currently-open tickets (up to `limit`).

    "Currently-open" = `status='open'`. The triage call flips status
    to 'investigating' so re-runs only pick up newly-created tickets.

    Returns the list of outcomes (one per triaged ticket).
    """
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        async with pool.acquire() as conn:
            # Block-3 RLS — bulk triage runs across the Default
            # Workspace by default.
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)",
                LEGACY_DEFAULT_TENANT_UUID,
            )
            ids = await conn.fetch(
                """
                SELECT ticket_id::text AS ticket_id
                  FROM ops.support_tickets
                 WHERE status = 'open'
                 ORDER BY reported_at ASC
                 LIMIT $1
                """,
                limit,
            )

        outcomes: list[TriageOutcome] = []
        for r in ids:
            try:
                outcome = await triage_ticket(
                    ticket_id=r["ticket_id"], pool=pool
                )
                outcomes.append(outcome)
            except Exception as e:
                log.warning(
                    "triage_unclassified_tickets.failed ticket=%s err=%s",
                    r["ticket_id"], e,
                )
        return outcomes
    finally:
        if owns_pool and pool is not None:
            await pool.close()


__all__ = [
    "TriageOutcome",
    "VALID_SEVERITIES",
    "VALID_CATEGORIES",
    "triage_ticket",
    "triage_unclassified_tickets",
]
