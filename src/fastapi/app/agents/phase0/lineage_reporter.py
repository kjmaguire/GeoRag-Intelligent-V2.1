"""Lineage Reporter Agent (Phase 0 agent #2).

Read-only walker over ``audit.audit_ledger``. Given a target (schema +
table + id, OR a workspace_id, OR an audit_ledger entry id), returns the
chain of audit entries that touched it — actor_id, action_type, payload,
created_at, hash, previous_hash — in chronological order.

Phase 0 target types: ``workflow_run``, ``audit_ledger_entry``, ``workspace``.
Document/passage/claim lineage lands when those tables ship in Phase 3+.

The agent is exposed as a FastAPI route (``/api/v1/lineage/{target_type}/{target_id}``)
in a follow-up wiring step; the function here is the cognitive payload.
"""

from __future__ import annotations

from typing import Any, Literal

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime

TargetType = Literal["workflow_run", "audit_ledger_entry", "workspace"]


@georag_agent(
    name="Lineage Reporter Agent",
    risk_tier="R0",
    version="0.1.0",
)
async def lineage_walk(
    ctx: AgentContext,
    *,
    target_type: TargetType,
    target_id: str,
    limit: int = 1000,
) -> dict[str, Any]:
    """Return the audit_ledger chain for a target.

    Returns:
        {
          "target_type": ...,
          "target_id": ...,
          "chain_length": int,
          "entries": [
            {
              "audit_id": uuid,
              "workspace_id": uuid|null,
              "actor_id": int|null,
              "actor_kind": str,
              "action_type": str,
              "created_at": iso,
              "hash_hex": str,
              "previous_hash_hex": str|null,
            }, ...
          ]
        }
    """
    rt = get_runtime()

    if target_type == "audit_ledger_entry":
        # Walk by id directly (single-entry chain, plus successors in same workspace).
        rows = await rt.pg_pool.fetch(
            """
            WITH anchor AS (
                SELECT workspace_id, created_at FROM audit.audit_ledger WHERE id = $1::uuid
            )
            SELECT id, workspace_id, actor_id, actor_kind, action_type,
                   target_schema, target_table, target_id,
                   created_at, hash, previous_hash
            FROM audit.audit_ledger
            WHERE workspace_id IS NOT DISTINCT FROM (SELECT workspace_id FROM anchor)
              AND created_at >= (SELECT created_at FROM anchor)
            ORDER BY created_at, id
            LIMIT $2
            """,
            target_id,
            limit,
        )
    elif target_type == "workspace":
        rows = await rt.pg_pool.fetch(
            """
            SELECT id, workspace_id, actor_id, actor_kind, action_type,
                   target_schema, target_table, target_id,
                   created_at, hash, previous_hash
            FROM audit.audit_ledger
            WHERE workspace_id = $1::uuid
            ORDER BY created_at, id
            LIMIT $2
            """,
            target_id,
            limit,
        )
    elif target_type == "workflow_run":
        rows = await rt.pg_pool.fetch(
            """
            SELECT id, workspace_id, actor_id, actor_kind, action_type,
                   target_schema, target_table, target_id,
                   created_at, hash, previous_hash
            FROM audit.audit_ledger
            WHERE target_schema = 'workflow' AND target_table = 'workflow_runs'
              AND target_id = $1
            ORDER BY created_at, id
            LIMIT $2
            """,
            target_id,
            limit,
        )
    else:
        raise ValueError(f"unsupported target_type: {target_type}")

    entries = [
        {
            "audit_id": str(r["id"]),
            "workspace_id": str(r["workspace_id"]) if r["workspace_id"] else None,
            "actor_id": r["actor_id"],
            "actor_kind": r["actor_kind"],
            "action_type": r["action_type"],
            "target_schema": r["target_schema"],
            "target_table": r["target_table"],
            "target_id": r["target_id"],
            "created_at": r["created_at"].isoformat(),
            "hash_hex": bytes(r["hash"]).hex() if r["hash"] else None,
            "previous_hash_hex": bytes(r["previous_hash"]).hex() if r["previous_hash"] else None,
        }
        for r in rows
    ]

    # All-nighter 2026-05-21 — broken-link continuity scan.
    # For every adjacent pair, the next row's previous_hash_hex must equal
    # the current row's hash_hex. A mismatch indicates either a quarantined
    # fork (see audit.audit_ledger_chain_fork_quarantine) or a real
    # tamper. We surface the positions; the caller decides severity.
    broken_at: list[dict[str, Any]] = []
    for i in range(1, len(entries)):
        prev_h = entries[i - 1]["hash_hex"]
        this_p = entries[i]["previous_hash_hex"]
        if prev_h is not None and this_p is not None and prev_h != this_p:
            broken_at.append({
                "idx": i,
                "audit_id": entries[i]["audit_id"],
                "expected_prev": prev_h,
                "got_prev": this_p,
            })

    return {
        "target_type": target_type,
        "target_id": target_id,
        "chain_length": len(entries),
        "broken_at": broken_at,
        "is_intact": len(broken_at) == 0,
        "entries": entries,
    }
