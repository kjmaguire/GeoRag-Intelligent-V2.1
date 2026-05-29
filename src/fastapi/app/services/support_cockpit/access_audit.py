"""Support access audit emission (§10.12 / §25.3) — doc-phase 116 LIVE.

When an ops user accesses customer workspace data through the
cockpit, this helper emits a `support_access` entry into the
hash-chained audit ledger. The workspace's audit ledger then
surfaces those entries to the workspace owner — so customers can
see exactly when support touched their data and why.

Pattern matches `app.audit.emit_audit` calls used in regular
state-changing flows. This module is the thin domain-specific
wrapper that locks the `action_type` namespace + the
controlled-vocabulary `access_kind` list.
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import asyncpg

from app.audit import AuditLedgerEntry, emit_audit


# Controlled vocabulary per §25.1 + scope proposal.
AccessKind = Literal[
    "workspace_state_view",
    "audit_ledger_excerpt",
    "workflow_replay_dry_run",
    "workflow_replay_live",
    "langfuse_trace_read",
    "report_read",
    "chat_history_read",
]


async def emit_support_access_audit(
    conn: asyncpg.Connection,
    *,
    workspace_id: UUID | str,
    ops_user_id: int,
    ticket_id: UUID | str | None,
    access_kind: AccessKind | str,
    target_summary: str,
    payload: dict[str, Any] | None = None,
) -> AuditLedgerEntry:
    """Record one cross-workspace access event.

    Args:
        conn: asyncpg Connection (caller manages transaction scope).
        workspace_id: the customer workspace whose data was touched.
        ops_user_id: the ops user who initiated access.
        ticket_id: optional ticket the access traces back to.
        access_kind: one of the controlled-vocabulary `AccessKind`
            values; a free-form string is also accepted for forward
            compatibility but logs a debug breadcrumb.
        target_summary: short human-readable description of what was
            accessed (shown to workspace owner).
        payload: structured payload (trace_ids, time window, replay_id,
            etc.) for forensic detail.

    Returns:
        AuditLedgerEntry — the new chain row's identifiers.
    """
    if not target_summary or not target_summary.strip():
        raise ValueError("target_summary is required")

    full_payload: dict[str, Any] = {
        "access_kind": access_kind,
        "target_summary": target_summary,
    }
    if ticket_id is not None:
        full_payload["ticket_id"] = str(ticket_id)
    if payload:
        # Caller payload merges on top — explicit keys win, but
        # internally-managed keys above are guaranteed present.
        full_payload = {**payload, **full_payload}

    return await emit_audit(
        conn,
        action_type="support_access",
        workspace_id=workspace_id,
        actor_id=ops_user_id,
        actor_kind="user",
        target_schema="ops",
        target_table="support_tickets" if ticket_id is not None else None,
        target_id=str(ticket_id) if ticket_id is not None else None,
        payload=full_payload,
    )


__all__ = ["AccessKind", "emit_support_access_audit"]
