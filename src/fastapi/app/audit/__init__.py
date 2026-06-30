"""GeoRAG audit ledger — Python emitter library (Phase 0 step 4.1).

The audit ledger is the system's tamper-evident record. Every state-changing
event must call ``emit_audit()`` — preferably inside the same transaction as
the state-changing write itself, so the audit row and the data row commit
together (or roll back together).

The hash chain is computed by a Postgres BEFORE-INSERT trigger
(``audit.compute_audit_hash``), not by this library. That keeps the recipe in
exactly one place — re-runnable from any client, including the verification
job. See ``docs/audit_ledger_hash_recipe.md``.

Typical usage::

    from app.audit import emit_audit

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await update_silver_row(conn, ...)
            await emit_audit(
                conn,
                action_type="silver.assay_results.update",
                workspace_id=workspace_id,
                actor_id=user_id,
                target_schema="silver",
                target_table="assay_results",
                target_id=str(row_id),
                payload={"changed_fields": ["au_ppm", "ag_ppm"]},
            )

The function returns the inserted row's id, hash, and previous_hash so
callers (e.g. ``outbox.propagation_attempts.audit_ledger_ref``) can reference
it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg

ActorKind = Literal["user", "system", "agent", "workflow", "external"]


@dataclass(frozen=True, slots=True)
class AuditLedgerEntry:
    """The minimal returned payload — full row stays in the database."""

    id: UUID
    workspace_id: UUID | None
    action_type: str
    hash: bytes
    previous_hash: bytes | None
    created_at: datetime


async def emit_audit(
    conn_or_pool: asyncpg.Connection | asyncpg.Pool,
    *,
    action_type: str,
    workspace_id: UUID | str | None = None,
    actor_id: int | None = None,
    actor_kind: ActorKind = "system",
    target_schema: str | None = None,
    target_table: str | None = None,
    target_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> AuditLedgerEntry:
    """Insert a row into ``audit.audit_ledger`` and return its identifiers.

    The Postgres trigger computes ``hash`` and ``previous_hash``; this function
    does not. Pass an existing ``asyncpg.Connection`` to participate in the
    caller's transaction; pass an ``asyncpg.Pool`` to use a one-shot
    autocommit connection.

    ``payload`` is serialised to JSON via asyncpg's default JSON codec; pass a
    plain ``dict`` (or any ``Mapping``) — keys must be ``str``, values must be
    JSON-serialisable.

    Raises ``asyncpg.PostgresError`` on insert failure (chain integrity is
    enforced by the trigger; concurrent inserts to the same workspace chain
    are serialised via row-level lock inside the trigger).
    """
    if not action_type:
        raise ValueError("action_type is required")

    sql = """
        INSERT INTO audit.audit_ledger (
            workspace_id, actor_id, actor_kind, action_type,
            target_schema, target_table, target_id,
            payload, trace_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
        RETURNING id, workspace_id, action_type, hash, previous_hash, created_at
    """

    # asyncpg encodes Python dicts as JSONB when the column type is jsonb;
    # but we cast explicitly so a TEXT-typed param doesn't get rejected.
    import json
    payload_json = json.dumps(dict(payload) if payload else {}, default=str, sort_keys=True)

    args = (
        str(workspace_id) if isinstance(workspace_id, UUID) else workspace_id,
        actor_id,
        actor_kind,
        action_type,
        target_schema,
        target_table,
        target_id,
        payload_json,
        trace_id,
    )

    # Block-3 RLS (2026-05-15): audit.audit_ledger WITH CHECK requires
    # the row's workspace_id to match the `app.workspace_id` GUC. To
    # keep emit_audit ergonomic for callers that haven't pre-set the
    # GUC (system-event paths, test fixtures), we temporarily align the
    # GUC to the row's workspace_id around the INSERT and restore the
    # caller's prior value on exit.
    ws_setting = (
        str(workspace_id) if isinstance(workspace_id, UUID)
        else (workspace_id or "")
    )

    async def _exec(conn: asyncpg.Connection):
        prior = await conn.fetchval(
            "SELECT current_setting('app.workspace_id', true)"
        )
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", ws_setting,
        )
        try:
            return await conn.fetchrow(sql, *args)
        finally:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)",
                prior or "",
            )

    if isinstance(conn_or_pool, asyncpg.Pool):
        async with conn_or_pool.acquire() as conn:
            row = await _exec(conn)
    else:
        row = await _exec(conn_or_pool)

    if row is None:  # pragma: no cover — RETURNING always yields a row on success
        raise RuntimeError("audit_ledger insert returned no row")

    entry = AuditLedgerEntry(
        id=row["id"],
        workspace_id=row["workspace_id"],
        action_type=row["action_type"],
        hash=bytes(row["hash"]) if row["hash"] is not None else b"",
        previous_hash=bytes(row["previous_hash"]) if row["previous_hash"] else None,
        created_at=row["created_at"],
    )

    # Phase 2 real-time staleness fix — every audit row whose action_type
    # ends in '.alert' or '.acknowledged' is surfaced live on the
    # Admin/AlertsInbox page. Hooking the central emit helper covers every
    # alert writer in one place (cost_burn_watcher, reliability_metrics_publisher,
    # stale_run_detector, vllm_security, plus future ones) without per-workflow
    # code edits.
    #
    # Mirror of the App\Services\Audit\AuditEmitter.php hook on the Laravel
    # side. Best-effort: a broadcast failure must NEVER fail the audit emit
    # — the durable record is the row already committed above.
    if action_type.endswith(".alert") or action_type.endswith(".acknowledged"):
        try:
            # Local import to avoid a top-level circular dep: laravel_bridge
            # may import config which may transitively import audit.
            from app.services.laravel_bridge import post_admin_surface_updated
            await post_admin_surface_updated(
                surface="alerts-inbox",
                affected_props=["items"],
                payload={
                    "audit_id": str(entry.id),
                    "action_type": action_type,
                    "workspace_id": str(entry.workspace_id) if entry.workspace_id else None,
                    "actor_kind": actor_kind,
                },
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "emit_audit: alerts-inbox broadcast failed audit_id=%s "
                "action_type=%s err=%s",
                entry.id, action_type, exc,
            )

    return entry


__all__ = ["emit_audit", "AuditLedgerEntry", "ActorKind"]
