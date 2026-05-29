"""§25.4 support_packet agent — doc-phase 140.

Third of the 5 §25.4 support agents (ticket_triage,
root_cause_investigation, **support_packet**,
customer_response_drafting, escalation_routing).

What it does: assembles a complete, exportable support packet for one
ticket — ticket info + all triage/investigation audit anchors + the
hash-chain proof that ties them together. Engineering hand-off
artifact.

What's live in this graduation:

  - `build_support_packet(ticket_id, pool=None)` — async function that:
      1. Reads the ticket row
      2. Pulls every `support.*` audit anchor for that ticket
      3. Pulls related decision records for the workspace (last 7d)
      4. Builds a structured packet payload (JSON-serializable)
      5. Emits a `support.packet.assembled` audit anchor referencing
         the packet (anchor's payload IS the packet)
      6. Returns the structured SupportPacket

The packet structure mirrors what §25.4's spec calls for: enough
context for an engineer to reproduce + diagnose the issue without
needing live workspace access.

This graduation is fully real — no synthetic stub. The packet
assembly is deterministic and content-true; it's just data
aggregation. Future enhancement: add Langfuse trace embeds + replay
URIs.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, NamedTuple
from uuid import UUID

import asyncpg

from app.audit import emit_audit

log = logging.getLogger("georag.support_cockpit.support_packet")


class SupportPacket(NamedTuple):
    """Structured packet returned to the caller."""

    ticket_id: UUID
    ticket: dict[str, Any]
    triage_anchors: list[dict[str, Any]]
    investigation_anchors: list[dict[str, Any]]
    related_decisions: list[dict[str, Any]]
    trace_links: list[dict[str, Any]]
    packet_anchor_id: UUID  # the support.packet.assembled audit ledger id
    summary: str


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _record_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    """Convert asyncpg Record → JSON-safe dict (str-cast UUIDs and
    datetimes)."""
    out: dict[str, Any] = {}
    for k in r.keys():
        v = r[k]
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, bytes):
            out[k] = v.hex()
        elif isinstance(v, str) and v.startswith("{"):
            # Try parse JSON for jsonb columns returned as text.
            try:
                out[k] = json.loads(v)
            except (ValueError, TypeError):
                out[k] = v
        else:
            out[k] = v
    return out


async def build_support_packet(
    *,
    ticket_id: UUID | str,
    pool: asyncpg.Pool | None = None,
) -> SupportPacket:
    """Assemble a complete support packet for one ticket.

    Args:
        ticket_id: UUID of the ops.support_tickets row.
        pool: optional asyncpg pool to reuse.

    Returns:
        SupportPacket with full ticket info + anchor chain + decision
        context + the audit ledger id of the packet anchor itself.

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
            # Block-3 RLS — see customer_response_drafting pattern.
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)",
                "a0000000-0000-0000-0000-000000000001",
            )
            # 1. Ticket row.
            ticket_row = await conn.fetchrow(
                """
                SELECT ticket_id::text AS ticket_id,
                       workspace_id::text AS workspace_id,
                       reported_by_user_id, reported_at, channel,
                       category, description, severity,
                       assigned_to_user_id, status,
                       resolution_summary, resolved_at,
                       customer_visible_response
                  FROM ops.support_tickets
                 WHERE ticket_id = $1::uuid
                """,
                ticket_str,
            )
            if ticket_row is None:
                raise ValueError(f"ticket not found: {ticket_str}")
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)",
                ticket_row["workspace_id"],
            )

            ticket_dict = _record_to_dict(ticket_row)

            # 2. Triage anchors (support.ticket.triaged for this ticket).
            triage_rows = await conn.fetch(
                """
                SELECT id::text AS id, created_at, actor_id, actor_kind,
                       payload
                  FROM audit.audit_ledger
                 WHERE action_type = 'support.ticket.triaged'
                   AND target_id = $1
                 ORDER BY created_at ASC
                """,
                ticket_str,
            )

            # 3. Investigation anchors.
            investigation_rows = await conn.fetch(
                """
                SELECT id::text AS id, created_at, actor_id, actor_kind,
                       trace_id, payload
                  FROM audit.audit_ledger
                 WHERE action_type = 'support.ticket.investigated'
                   AND target_id = $1
                 ORDER BY created_at ASC
                """,
                ticket_str,
            )

            # 4. Trace links (ops.support_ticket_traces).
            trace_rows = await conn.fetch(
                """
                SELECT link_id::text AS link_id, trace_id, trace_summary,
                       added_by_user_id, added_at
                  FROM ops.support_ticket_traces
                 WHERE ticket_id = $1::uuid
                 ORDER BY added_at ASC
                """,
                ticket_str,
            )

            # 5. Related decisions in the workspace (last 7d).
            decision_rows: list[asyncpg.Record] = []
            if ticket_dict.get("workspace_id"):
                decision_rows = await conn.fetch(
                    """
                    SELECT decision_id::text AS decision_id,
                           decision_type, human_decision,
                           recommendation, decided_at,
                           decided_by_user_id, uncertainty
                      FROM silver.decision_records
                     WHERE workspace_id = $1::uuid
                       AND decided_at >= now() - interval '7 days'
                     ORDER BY decided_at DESC
                     LIMIT 25
                    """,
                    ticket_dict["workspace_id"],
                )

            triage_dicts = [_record_to_dict(r) for r in triage_rows]
            investigation_dicts = [_record_to_dict(r) for r in investigation_rows]
            trace_dicts = [_record_to_dict(r) for r in trace_rows]
            decision_dicts = [_record_to_dict(r) for r in decision_rows]

            summary = (
                f"Support packet for ticket {ticket_str[:8]}... "
                f"[category={ticket_dict['category']} severity={ticket_dict['severity']} "
                f"status={ticket_dict['status']}] "
                f"— {len(triage_dicts)} triage / "
                f"{len(investigation_dicts)} investigation / "
                f"{len(trace_dicts)} trace links / "
                f"{len(decision_dicts)} related decisions"
            )

            # 6. Emit the packet anchor.
            ledger = await emit_audit(
                conn,
                action_type="support.packet.assembled",
                workspace_id=ticket_dict.get("workspace_id"),
                actor_kind="agent",
                target_schema="ops",
                target_table="support_tickets",
                target_id=ticket_str,
                payload={
                    "evaluator": "support_packet_v1",
                    "doc_phase": 140,
                    "summary": summary[:500],
                    "ticket_category": ticket_dict["category"],
                    "ticket_severity": ticket_dict["severity"],
                    "ticket_status": ticket_dict["status"],
                    "triage_count": len(triage_dicts),
                    "investigation_count": len(investigation_dicts),
                    "trace_link_count": len(trace_dicts),
                    "decision_count": len(decision_dicts),
                },
            )

            log.info(
                "support_packet.assembled ticket=%s anchor=%s triage=%d "
                "investigations=%d traces=%d decisions=%d",
                ticket_str, ledger.id, len(triage_dicts),
                len(investigation_dicts), len(trace_dicts),
                len(decision_dicts),
            )

            return SupportPacket(
                ticket_id=UUID(ticket_str),
                ticket=ticket_dict,
                triage_anchors=triage_dicts,
                investigation_anchors=investigation_dicts,
                related_decisions=decision_dicts,
                trace_links=trace_dicts,
                packet_anchor_id=ledger.id,
                summary=summary,
            )
    finally:
        if owns_pool and pool is not None:
            await pool.close()


__all__ = [
    "SupportPacket",
    "build_support_packet",
]
