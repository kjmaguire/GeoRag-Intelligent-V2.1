"""record_decision — single facade for §21 decision capture.

Doc-phase 115 LIVE implementation (was doc-phase 92 skeleton). The
eight §21.3 decision types funnel through this one function; each
call atomically:

1. INSERTs into `silver.decision_records`
2. INSERTs evidence links into `silver.decision_evidence_links`
3. INSERTs options into `silver.decision_options`
4. (Optionally) INSERTs an outcome row into `silver.decision_outcomes`
5. Emits an `audit.audit_ledger` row via `app.audit.emit_audit` —
   the audit row id is recorded on the decision_records row.

All steps run inside the caller's transaction. If the caller
hasn't opened one, the function opens one itself.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

import asyncpg

from app.audit import emit_audit

DecisionType = Literal[
    "target_recommendation",
    "crs_decision",
    "schema_mapping",
    "public_data_import",
    "export_approval",
    "workflow_enablement",
    "conflict_resolution",
    "report_signoff",
]


async def record_decision(
    conn: asyncpg.Connection,
    *,
    workspace_id: UUID | str,
    decision_type: DecisionType,
    recommendation: str,
    human_decision: str,
    decided_by_user_id: int,
    reason: str | None = None,
    uncertainty: float | None = None,
    evidence_chunk_ids: Sequence[str] = (),
    options_considered: Sequence[dict[str, Any]] = (),
    outcome_kind: str | None = None,
    outcome_payload: dict[str, Any] | None = None,
) -> UUID:
    """Record one §21 decision with all links + audit emission.

    Args:
        conn: asyncpg Connection. The function opens its own
            transaction if the caller hasn't.
        workspace_id: workspace RLS scope.
        decision_type: one of the 8 §21.3 types.
        recommendation: AI / system recommendation text.
        human_decision: human's chosen action.
        decided_by_user_id: public.users.id of the decider.
        reason: optional human rationale.
        uncertainty: declared uncertainty at decision time (0-1).
        evidence_chunk_ids: supporting source chunk ids.
        options_considered: list of `{label, description, was_chosen,
            payload?}` dicts.
        outcome_kind: optional post-decision outcome kind.
        outcome_payload: optional outcome details.

    Returns:
        decision_id (UUID).
    """
    if uncertainty is not None and not (0 <= uncertainty <= 1):
        raise ValueError(
            f"uncertainty must be in [0, 1] when set; got {uncertainty}"
        )

    workspace_str = str(workspace_id) if isinstance(workspace_id, UUID) else workspace_id

    async def _do_work() -> UUID:
        # 1. INSERT decision_records — get the new decision_id back.
        decision_id = await conn.fetchval(
            """
            INSERT INTO silver.decision_records (
                workspace_id, decision_type, recommendation, human_decision,
                reason, uncertainty, decided_by_user_id
            )
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
            RETURNING decision_id
            """,
            workspace_str,
            decision_type,
            recommendation,
            human_decision,
            reason,
            uncertainty,
            decided_by_user_id,
        )

        # 2. INSERT evidence links (Block-2 RLS: carry workspace_id explicitly)
        for chunk_id in evidence_chunk_ids:
            await conn.execute(
                """
                INSERT INTO silver.decision_evidence_links (
                    decision_id, source_chunk_id, role, workspace_id
                )
                VALUES ($1::uuid, $2, 'supporting', $3::uuid)
                """,
                str(decision_id),
                chunk_id,
                workspace_str,
            )

        # 3. INSERT options considered
        for opt in options_considered:
            label = opt.get("label")
            description = opt.get("description", "")
            was_chosen = bool(opt.get("was_chosen", False))
            payload = opt.get("payload", {})
            if not label:
                raise ValueError("each option must have a 'label' field")
            await conn.execute(
                """
                INSERT INTO silver.decision_options (
                    decision_id, label, description, was_chosen, payload, workspace_id
                )
                VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::uuid)
                """,
                str(decision_id),
                label,
                description,
                was_chosen,
                json.dumps(payload, default=str),
                workspace_str,
            )

        # 4. Optional outcome row
        if outcome_kind is not None:
            await conn.execute(
                """
                INSERT INTO silver.decision_outcomes (
                    decision_id, outcome_kind, outcome_payload, workspace_id
                )
                VALUES ($1::uuid, $2, $3::jsonb, $4::uuid)
                """,
                str(decision_id),
                outcome_kind,
                json.dumps(outcome_payload or {}, default=str),
                workspace_str,
            )

        # 5. Emit audit ledger row + back-fill audit_ledger_id on the
        # decision row.
        ledger_entry = await emit_audit(
            conn,
            action_type=f"decision.{decision_type}",
            workspace_id=workspace_str,
            actor_id=decided_by_user_id,
            actor_kind="user",
            target_schema="silver",
            target_table="decision_records",
            target_id=str(decision_id),
            payload={
                "decision_type": decision_type,
                "human_decision": human_decision,
                "evidence_count": len(evidence_chunk_ids),
                "options_count": len(options_considered),
            },
        )

        await conn.execute(
            """
            UPDATE silver.decision_records
            SET audit_ledger_id = $2::uuid,
                hash = $3
            WHERE decision_id = $1::uuid
            """,
            str(decision_id),
            str(ledger_entry.id),
            ledger_entry.hash,
        )

        return decision_id

    # If the caller is already in a transaction, just run; otherwise
    # open one. asyncpg's `Connection.transaction()` is re-entrant
    # via savepoints, so this is safe either way.
    async with conn.transaction():
        return await _do_work()


__all__ = ["DecisionType", "record_decision"]
