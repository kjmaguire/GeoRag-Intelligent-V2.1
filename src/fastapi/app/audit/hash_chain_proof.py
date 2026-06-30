"""Hash chain proof JSON generator (§7.7 / §15.3) — doc-phase 117 LIVE.

Every report bundle ships with a `hash_chain_proof.json` so an
external auditor can independently verify the report's integrity
against `audit.audit_ledger`.

Per master-plan §15.3, the proof JSON commits to:
- every audit_ledger row tied to the report's lifecycle within
  the verification window
- each row's stored hash + the recomputed hash from §22 recipe
- the verification range used (workspace_id, time window)

The proof is verifiable WITHOUT GeoRAG code by reading the JSON,
re-running the recipe from `docs/audit_ledger_hash_recipe.md`, and
comparing against the embedded hashes. If any row was tampered with
post-generation, the recomputed hash diverges.

Doc-phase 117 — live implementation. Uses the existing
`audit.verify_hash_chain(start, end)` SQL function to recompute
hashes server-side (single source of truth — same code path the
nightly verifier uses).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

RECIPE_VERSION = "v1"


async def build_hash_chain_proof(
    conn: asyncpg.Connection,
    *,
    report_id: UUID | str | None = None,
    workspace_id: UUID | str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Assemble the hash_chain_proof.json payload for a verification window.

    Queries `audit.audit_ledger` for every row in the workspace +
    time window. For each row, re-runs the §22 hash recipe via
    `audit.verify_hash_chain(start, end)` (server-side, deterministic)
    and embeds both stored + recomputed hashes so an external auditor
    can re-verify without GeoRAG application code.

    Args:
        conn: asyncpg Connection scoped to the workspace's RLS.
        report_id: optional — when supplied, filters audit rows whose
            payload references the report_id. NULL = entire window.
        workspace_id: workspace for chain scoping.
        start: lower bound of the verification window (inclusive).
        end: upper bound (typically NOW() at proof-generation time).

    Returns:
        Proof dict ready for `json.dumps()`. See module docstring
        for schema.
    """
    if end <= start:
        raise ValueError(f"end ({end}) must be > start ({start})")

    workspace_str = str(workspace_id) if isinstance(workspace_id, UUID) else workspace_id

    # Pull ledger rows (workspace-scoped via RLS; falls back to direct
    # workspace_id filter for defense in depth).
    base_sql = """
        SELECT
            id,
            workspace_id,
            actor_id,
            actor_kind,
            action_type,
            target_schema,
            target_table,
            target_id,
            payload::text AS payload_text,
            previous_hash,
            hash AS stored_hash,
            to_char(created_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS created_at_iso
        FROM audit.audit_ledger
        WHERE workspace_id = $1::uuid
          AND created_at >= $2
          AND created_at < $3
    """
    args: list[Any] = [workspace_str, start, end]

    if report_id is not None:
        # Match audit rows whose payload references the report_id
        # OR whose target_id matches it (so target_schema='silver',
        # target_table='reports', target_id=<report_id> rows match).
        base_sql += " AND (payload->>'report_id' = $4 OR target_id = $4)"
        args.append(str(report_id))

    base_sql += " ORDER BY created_at ASC, id ASC"

    rows = await conn.fetch(base_sql, *args)

    # Recompute each row's hash + verify match. The §22 recipe is
    # implemented in Postgres as `audit.recompute_hash(...)`; calling
    # it per row keeps the implementation single-source-of-truth.
    proof_rows: list[dict[str, Any]] = []
    broken_ids: list[str] = []

    for r in rows:
        recomputed = await conn.fetchval(
            """
            SELECT audit.recompute_hash(
                $1::bytea, $2::bigint, $3::text, $4::text,
                $5::text, $6::text, $7::text, $8::jsonb, $9::timestamptz
            )
            """,
            r["previous_hash"],
            r["actor_id"],
            r["actor_kind"],
            r["action_type"],
            r["target_schema"],
            r["target_table"],
            r["target_id"],
            r["payload_text"],
            datetime.fromisoformat(r["created_at_iso"].replace("Z", "+00:00")),
        )

        stored_hex = r["stored_hash"].hex() if r["stored_hash"] else ""
        recomputed_hex = recomputed.hex() if recomputed else ""
        prev_hex = r["previous_hash"].hex() if r["previous_hash"] else ""
        match = stored_hex == recomputed_hex

        if not match:
            broken_ids.append(str(r["id"]))

        # Try to coerce payload back to dict for the JSON proof
        # (gives external auditors easier consumption).
        try:
            payload_obj = json.loads(r["payload_text"])
        except (json.JSONDecodeError, TypeError):
            payload_obj = None

        proof_rows.append({
            "id": str(r["id"]),
            "created_at": r["created_at_iso"],
            "action_type": r["action_type"],
            "actor_kind": r["actor_kind"],
            "actor_id": r["actor_id"],
            "target_schema": r["target_schema"],
            "target_table": r["target_table"],
            "target_id": r["target_id"],
            "payload_text": r["payload_text"],
            "payload": payload_obj,
            "previous_hash_hex": prev_hex,
            "stored_hash_hex": stored_hex,
            "recomputed_hash_hex": recomputed_hex,
            "match": match,
        })

    return {
        "report_id": str(report_id) if report_id is not None else None,
        "workspace_id": workspace_str,
        "recipe_version": RECIPE_VERSION,
        "verification_range": {
            "start": start.astimezone().isoformat().replace("+00:00", "Z"),
            "end": end.astimezone().isoformat().replace("+00:00", "Z"),
        },
        "rows": proof_rows,
        "summary": {
            "row_count": len(proof_rows),
            "all_match": len(broken_ids) == 0,
            "broken_ids": broken_ids,
        },
    }


__all__ = ["build_hash_chain_proof", "RECIPE_VERSION"]
