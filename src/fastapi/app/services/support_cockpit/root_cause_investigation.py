"""§25.4 root_cause_investigation agent — doc-phase 139.

Second of the 5 §25.4 support agents. Runs against a triaged ticket
(status='investigating' typically; the agent re-runs are also OK).

What's live in this graduation:

  - `investigate_ticket()` — async function that:
      1. Reads the ticket
      2. Pulls recent audit entries + decision records + (optional)
         failed workflow runs for the ticket's workspace
      3. Runs heuristic pattern matching to identify probable causes
      4. Builds a structured investigation payload (top_causes,
         relevant_audit_ids, relevant_decision_ids)
      5. Links the investigation via `ops.support_ticket_traces`
         (trace_id = the investigation's id; trace_summary = top
         cause string)
      6. Emits `support.ticket.investigated` audit anchor with the
         full structured payload

The heuristic pattern matcher is the synthetic-stub piece. Real LLM
root-cause analysis lands in a future tick; the orchestration around
it doesn't change.

Pattern rules (synthetic):
  - category='failed_ingestion'   → look for recent `ingest_pdf.*` audit anchors
  - category='failed_report'      → look for recent `report.*` audit anchors
  - category='integration_issue'  → look for recent `external_notification.*` and
                                     `public_geo.pull.*` failures
  - category='wrong_answer'       → look at recent decisions (decision.*) in the workspace
  - category='performance'        → no specific anchor pattern; report data scarcity
  - category='other'              → no specific anchor pattern; report data scarcity
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import asyncpg

from app.audit import emit_audit
from app.db import lookup_and_rescope

log = logging.getLogger("georag.support_cockpit.root_cause_investigation")


# Category → audit action_type prefix patterns to scan.
CATEGORY_AUDIT_PATTERNS: dict[str, list[str]] = {
    "failed_ingestion": ["ingest_pdf.", "ingest.", "ocr."],
    "failed_report": ["report.", "generate_report."],
    "integration_issue": [
        "external_notification.", "public_geo.pull.",
        "workflow.jwt_key.", "usage.external_notification_sender.",
    ],
    "wrong_answer": ["decision.", "hypothesis."],
    "performance": [],
    "other": [],
}


class InvestigationResult(NamedTuple):
    """Result of one investigation."""

    ticket_id: UUID
    trace_id: str  # the investigation's trace_id (UUID4 hex prefix)
    top_cause_summary: str
    top_causes: list[dict[str, Any]]  # ranked list with cause + relevance + evidence
    relevant_audit_ids: list[str]
    relevant_decision_ids: list[str]
    investigation_method: str  # 'synthetic_stub' | 'llm' | ...


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _scan_recent_audits(
    conn: asyncpg.Connection,
    workspace_id: str | None,
    action_prefixes: list[str],
    lookback_hours: int = 168,  # 7 days
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Pull recent audit_ledger rows whose action_type starts with any
    of the provided prefixes, scoped to the ticket's workspace when
    available."""
    if not action_prefixes:
        return []

    # Use ANY with LIKE patterns.
    prefix_patterns = [p + "%" for p in action_prefixes]

    if workspace_id is not None:
        return await conn.fetch(
            """
            SELECT id::text AS id, action_type, target_id,
                   created_at, payload
              FROM audit.audit_ledger
             WHERE workspace_id = $1::uuid
               AND created_at >= now() - make_interval(hours => $2)
               AND action_type LIKE ANY($3::text[])
             ORDER BY created_at DESC
             LIMIT $4
            """,
            workspace_id, lookback_hours, prefix_patterns, limit,
        )
    return await conn.fetch(
        """
        SELECT id::text AS id, action_type, target_id,
               created_at, payload
          FROM audit.audit_ledger
         WHERE created_at >= now() - make_interval(hours => $1)
           AND action_type LIKE ANY($2::text[])
         ORDER BY created_at DESC
         LIMIT $3
        """,
        lookback_hours, prefix_patterns, limit,
    )


async def _scan_recent_decisions(
    conn: asyncpg.Connection,
    workspace_id: str | None,
    lookback_hours: int = 168,
    limit: int = 20,
) -> list[asyncpg.Record]:
    """Pull recent silver.decision_records for the workspace."""
    if workspace_id is None:
        return []
    return await conn.fetch(
        """
        SELECT decision_id::text AS id, decision_type, human_decision,
               recommendation, decided_at
          FROM silver.decision_records
         WHERE workspace_id = $1::uuid
           AND decided_at >= now() - make_interval(hours => $2)
         ORDER BY decided_at DESC
         LIMIT $3
        """,
        workspace_id, lookback_hours, limit,
    )


def _synthesize_top_causes(
    category: str,
    audit_rows: list[asyncpg.Record],
    decision_rows: list[asyncpg.Record],
) -> tuple[list[dict[str, Any]], str]:
    """Heuristic synthesis of top causes from the gathered evidence.

    Returns (top_causes, top_cause_summary).

    Each cause is a dict:
        {
            "cause": str,
            "relevance": float (0..1),
            "evidence_audit_ids": [str],
            "evidence_decision_ids": [str],
        }
    """
    causes: list[dict[str, Any]] = []

    # Group audits by action_type for clustering.
    audits_by_action: dict[str, list[asyncpg.Record]] = {}
    for r in audit_rows:
        audits_by_action.setdefault(r["action_type"], []).append(r)

    for action_type, rows in sorted(
        audits_by_action.items(), key=lambda kv: -len(kv[1])
    ):
        relevance = min(1.0, 0.2 + 0.1 * len(rows))
        causes.append({
            "cause": (
                f"Recent {action_type} events ({len(rows)}× in last 7 days) "
                f"may relate to this ticket."
            ),
            "relevance": round(relevance, 3),
            "evidence_audit_ids": [r["id"] for r in rows[:5]],
            "evidence_decision_ids": [],
        })

    if category == "wrong_answer" and decision_rows:
        causes.append({
            "cause": (
                f"Recent decisions in workspace ({len(decision_rows)}× in last 7 days) "
                f"may shape the chat response."
            ),
            "relevance": 0.55,
            "evidence_audit_ids": [],
            "evidence_decision_ids": [r["id"] for r in decision_rows[:5]],
        })

    if not causes:
        causes.append({
            "cause": (
                f"No directly-relevant audit signal found in the 7-day window "
                f"for category='{category}'. Recommend manual review or "
                f"escalation to ops."
            ),
            "relevance": 0.1,
            "evidence_audit_ids": [],
            "evidence_decision_ids": [],
        })

    # Sort by relevance DESC.
    causes.sort(key=lambda c: -c["relevance"])

    top_cause_summary = causes[0]["cause"]
    return causes, top_cause_summary


async def investigate_ticket(
    *,
    ticket_id: UUID | str,
    actor_user_id: int,
    lookback_hours: int = 168,
    pool: asyncpg.Pool | None = None,
) -> InvestigationResult:
    """Run a synthetic root-cause investigation against the ticket.

    Args:
        ticket_id: UUID of the support_tickets row.
        actor_user_id: public.users.id of the investigating user
            (or a service user). Required for the
            `support_ticket_traces.added_by_user_id` FK.
        lookback_hours: how far back to scan audit/decision data.
        pool: optional asyncpg pool to reuse.

    Returns:
        InvestigationResult with top_cause_summary, top_causes,
        relevant_audit_ids, relevant_decision_ids, and a generated
        trace_id linking this investigation.
    """
    ticket_str = str(ticket_id) if isinstance(ticket_id, UUID) else ticket_id

    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        # ADR-0014 lookup_and_rescope — see customer_response_drafting.py
        # for the reference migration.
        async with lookup_and_rescope(
            pool,
            lookup_sql="""
                SELECT ticket_id, workspace_id::text AS workspace_id,
                       description, severity, category, status
                  FROM ops.support_tickets
                 WHERE ticket_id = $1::uuid
                """,
            lookup_args=(ticket_str,),
            site="support_cockpit.root_cause_investigation",
            bootstrap_reason="support_cockpit.elevated_lookup",
        ) as (conn, ticket):
            # 2. Scan recent audit + decision signal.
            patterns = CATEGORY_AUDIT_PATTERNS.get(ticket["category"], [])
            audit_rows = await _scan_recent_audits(
                conn, ticket["workspace_id"], patterns, lookback_hours
            )
            decision_rows = await _scan_recent_decisions(
                conn, ticket["workspace_id"], lookback_hours
            )

            # 3. Synthesize top causes.
            top_causes, top_summary = _synthesize_top_causes(
                ticket["category"], audit_rows, decision_rows
            )

            # 4. Generate an investigation trace_id (synthetic).
            #    Real impl would correlate to a Langfuse trace.
            seed = hashlib.sha256(
                f"investigation__{ticket_str}__{uuid4()}".encode()
            ).hexdigest()
            trace_id = f"inv_{seed[:16]}"

            relevant_audit_ids = [r["id"] for r in audit_rows]
            relevant_decision_ids = [r["id"] for r in decision_rows]

            # 5. Link the investigation via support_ticket_traces.
            #    Use INSERT ... ON CONFLICT DO NOTHING for idempotency
            #    on the unique (ticket_id, trace_id) — re-runs against
            #    the same trace_id are no-ops.
            await conn.execute(
                """
                INSERT INTO ops.support_ticket_traces (
                    ticket_id, trace_id, trace_summary, added_by_user_id
                )
                VALUES ($1::uuid, $2, $3, $4)
                ON CONFLICT (ticket_id, trace_id) DO NOTHING
                """,
                ticket_str, trace_id, top_summary[:500], actor_user_id,
            )

            # 6. Audit anchor with full structured payload.
            await emit_audit(
                conn,
                action_type="support.ticket.investigated",
                workspace_id=ticket["workspace_id"],
                actor_id=actor_user_id,
                actor_kind="agent",
                target_schema="ops",
                target_table="support_tickets",
                target_id=ticket_str,
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": 139,
                    "category": ticket["category"],
                    "severity": ticket["severity"],
                    "trace_id": trace_id,
                    "lookback_hours": lookback_hours,
                    "top_causes_count": len(top_causes),
                    "top_cause_summary": top_summary[:500],
                    "relevant_audit_ids_count": len(relevant_audit_ids),
                    "relevant_decision_ids_count": len(relevant_decision_ids),
                },
                trace_id=trace_id,
            )

            log.info(
                "root_cause_investigation.completed ticket=%s trace=%s "
                "audits_scanned=%d decisions_scanned=%d causes=%d",
                ticket_str, trace_id, len(audit_rows), len(decision_rows),
                len(top_causes),
            )

            return InvestigationResult(
                ticket_id=UUID(ticket_str),
                trace_id=trace_id,
                top_cause_summary=top_summary,
                top_causes=top_causes,
                relevant_audit_ids=relevant_audit_ids,
                relevant_decision_ids=relevant_decision_ids,
                investigation_method="synthetic_stub",
            )
    finally:
        if owns_pool and pool is not None:
            await pool.close()


__all__ = [
    "CATEGORY_AUDIT_PATTERNS",
    "InvestigationResult",
    "investigate_ticket",
]
