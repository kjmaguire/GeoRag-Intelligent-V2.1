"""Support Packet Agent (§10.9 / §25.4 + §15.4).

Assembles a diagnostic bundle on ticket fire OR report-generation
failure. Phase G.5 minimum-viable body: fetches the ticket row + a
window of recent audit-ledger anchors, answer_runs, and
workflow_runs scoped to the ticket's workspace. SeaweedFS upload of
the bundle is deferred to §15.4 follow-up.
"""
from __future__ import annotations
from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID

import os
from datetime import datetime
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
    name="Support Packet Agent",
    risk_tier="R2",
    version="0.2.0",
)
async def support_packet(
    ctx: AgentContext,
    *,
    ticket_id: UUID | str | None = None,
    failure_context: dict[str, Any] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    include_audit_anchors: int = 10,
    include_recent_runs: int = 5,
) -> dict[str, Any]:
    """Build a diagnostic bundle for one support ticket.

    Returns:
        {
            "ticket_id": "<uuid>",
            "ticket": {…},                          — ticket row
            "workspace_id": "<uuid>",
            "recent_audit_anchors": [{…}, …],
            "recent_answer_runs": [{…}, …],
            "recent_workflow_runs": [{…}, …],
            "audit_anchor_count_30d": <int>,
        }

    Phase G.5 MVP — SeaweedFS upload of the bundle is deferred.
    The dict shape is stable so a future tick can persist it.
    """
    if ticket_id is None:
        return {
            "error": "ticket_id required",
            "failure_context": failure_context,
        }

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Block-3 RLS — ops.support_tickets is workspace_id-scoped.
        ws = str(ctx.workspace_id) if ctx and ctx.workspace_id \
             else LEGACY_DEFAULT_TENANT_UUID
        # Audit 2026-06-28: SET LOCAL in a transaction (PgBouncer tx-mode: a
        # session-scoped set_config leaks the workspace GUC to the next client).
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)", ws,
            )
            ticket = await conn.fetchrow(
                """
                SELECT ticket_id::text AS id,
                       workspace_id::text AS workspace_id,
                       reported_by_user_id, assigned_to_user_id,
                       channel, category, severity, status,
                       description,
                       reported_at::text AS reported_at,
                       resolved_at::text AS resolved_at
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

        workspace_id = ticket["workspace_id"]
        anchors: list[dict] = []
        runs_answer: list[dict] = []
        runs_workflow: list[dict] = []
        anchor_count_30d = 0

        if workspace_id:
            try:
                anchor_rows = await conn.fetch(
                    """
                    SELECT id::text AS id, action_type, actor_kind,
                           created_at::text AS created_at,
                           target_schema, target_table
                      FROM audit.audit_ledger
                     WHERE workspace_id = $1::uuid
                     ORDER BY created_at DESC
                     LIMIT $2
                    """,
                    workspace_id, include_audit_anchors,
                )
                anchors = [dict(r) for r in anchor_rows]
            except Exception:
                anchors = []

            try:
                anchor_count_30d = await conn.fetchval(
                    """
                    SELECT count(*)::int FROM audit.audit_ledger
                     WHERE workspace_id = $1::uuid
                       AND created_at >= NOW() - INTERVAL '30 days'
                    """,
                    workspace_id,
                ) or 0
            except Exception:
                anchor_count_30d = 0

            try:
                ans_rows = await conn.fetch(
                    """
                    SELECT answer_run_id::text AS id,
                           citation_lifecycle_state AS state,
                           created_at::text AS created_at
                      FROM silver.answer_runs
                     WHERE workspace_id = $1::uuid
                     ORDER BY created_at DESC
                     LIMIT $2
                    """,
                    workspace_id, include_recent_runs,
                )
                runs_answer = [dict(r) for r in ans_rows]
            except Exception:
                runs_answer = []

            try:
                wf_rows = await conn.fetch(
                    """
                    SELECT run_id::text AS id,
                           workflow_kind AS workflow_name, status,
                           started_at::text AS created_at
                      FROM workflow.workflow_runs
                     WHERE workspace_id = $1::uuid
                     ORDER BY started_at DESC
                     LIMIT $2
                    """,
                    workspace_id, include_recent_runs,
                )
                runs_workflow = [dict(r) for r in wf_rows]
            except Exception:
                runs_workflow = []
    finally:
        await conn.close()

    bundle = {
        "ticket_id": ticket["id"],
        "ticket": dict(ticket),
        "workspace_id": ticket["workspace_id"],
        "recent_audit_anchors": anchors,
        "recent_answer_runs": runs_answer,
        "recent_workflow_runs": runs_workflow,
        "audit_anchor_count_30d": anchor_count_30d,
        "failure_context": failure_context,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
    }

    # Phase G overnight — wire to Kestra. No-op when KESTRA_URL is unset
    # (default). When configured, the dispatcher posts the full bundle to
    # ${KESTRA_URL}/api/v1/executions/{namespace}/{flow_id} so Slack /
    # SeaweedFS / audit-attach flows can run downstream. The dispatch
    # result is attached to the bundle so the cockpit UI can show
    # whether the outbound trigger actually fired.
    try:
        from app.services.dispatchers import (  # noqa: PLC0415
            dispatch_support_packet_to_kestra,
        )
        dispatch_result = await dispatch_support_packet_to_kestra(bundle)
    except Exception as exc:  # noqa: BLE001 — never let a dispatcher error fail the agent
        dispatch_result = {
            "dispatched": False,
            "reason": "dispatcher_exception",
            "error": f"{type(exc).__name__}: {exc}",
        }
    bundle["kestra_dispatch"] = dispatch_result
    return bundle
