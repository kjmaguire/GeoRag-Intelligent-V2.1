"""Audit-ledger hash-chain integrity verification.

The `audit.audit_ledger` table maintains a tamper-evident chain via the
`audit.compute_audit_hash` BEFORE INSERT trigger. Each row's `hash`
column commits to `previous_hash` plus the row content (recipe in
`docs/audit_ledger_hash_recipe.md`).

This module provides an on-demand verifier — operators can:
  - smoke a window after a suspicious deploy
  - run it nightly via cron against the prior 24h
  - back-validate before a Phase 0 cold-tier archive
without going through the archive path (which only verifies the
window it's about to upload).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg


@dataclass(frozen=True)
class ChainVerifyResult:
    """Outcome of a single verification pass.

    - `rows_verified` — count of rows examined.
    - `continuous` — True if every row[i+1].previous_hash == row[i].hash
      (the first row's previous_hash is not checked — it links to the
      genesis row or a prior partition we don't read here).
    - `failure_reason` — None when continuous; otherwise a one-line
      description of the first break encountered (the chain walk halts
      at the first failure).
    - `first_break_id` — the audit_ledger.id at which the break was
      detected, or None.
    - `quarantined_skipped` — count of rows the verifier ignored because
      they appear in `audit.audit_ledger_chain_fork_quarantine` (the
      2026-05-19 advisory-lock-incident forks; see
      `docs/runbooks/audit_ledger_rehash_2026_05_19.md`). The chain is
      considered intact AT quarantined rows because audit history is
      append-only and the divergence is recorded, not silently passed.
    """

    rows_verified: int
    continuous: bool
    failure_reason: str | None
    first_break_id: str | None
    window_start: datetime | None
    window_end: datetime | None
    quarantined_skipped: int = 0


async def verify_chain_window(
    conn: asyncpg.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    workspace_id_scope: str | None = None,
    limit: int = 100_000,
) -> ChainVerifyResult:
    """Walk audit.audit_ledger rows in `created_at, id` order and check
    every row[i+1].previous_hash matches row[i].hash.

    Args:
        conn: an asyncpg connection (caller manages transaction).
        since: lower bound on `created_at` (inclusive). None = no lower bound.
        until: upper bound on `created_at` (exclusive). None = no upper bound.
        workspace_id_scope: optional — verify only one workspace's chain.
            Note: cross-workspace events (workspace_id IS NULL) are
            INCLUDED in the global walk; passing a workspace_id_scope
            walks only that workspace.
        limit: hard cap on rows fetched (default 100k). The verifier
            short-circuits on the first break.

    Returns:
        ChainVerifyResult.
    """
    # Column references are al.<col> because the query LEFT JOINs the
    # quarantine table below; bare names would be ambiguous.
    where: list[str] = []
    params: list[Any] = []
    pi = 1
    if since is not None:
        where.append(f"al.created_at >= ${pi}")
        params.append(since)
        pi += 1
    if until is not None:
        where.append(f"al.created_at < ${pi}")
        params.append(until)
        pi += 1
    if workspace_id_scope is not None:
        where.append(f"al.workspace_id = ${pi}::uuid")
        params.append(workspace_id_scope)
        pi += 1
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # LEFT JOIN the fork-quarantine table so the walker knows which rows
    # are EXPECTED-divergent. A NULL `quarantined` flag means the row
    # must chain cleanly; TRUE means the row is one of the 1,171 forks
    # from the 2026-05-19 advisory-lock incident and the previous_hash
    # mismatch is a recorded fact, not tampering. See
    # docs/runbooks/audit_ledger_rehash_2026_05_19.md.
    rows = await conn.fetch(
        f"""
        SELECT al.id::text           AS id,
               al.hash                AS hash,
               al.previous_hash       AS previous_hash,
               al.created_at          AS created_at,
               (q.row_id IS NOT NULL) AS quarantined
          FROM audit.audit_ledger al
          LEFT JOIN audit.audit_ledger_chain_fork_quarantine q
                 ON q.row_id = al.id
         {where_sql}
         ORDER BY al.created_at ASC, al.id ASC
         LIMIT ${pi}
        """,
        *params,
        limit,
    )

    if not rows:
        return ChainVerifyResult(
            rows_verified=0,
            continuous=True,
            failure_reason=None,
            first_break_id=None,
            window_start=since,
            window_end=until,
            quarantined_skipped=0,
        )

    prev: dict[str, Any] | None = None
    examined = 0
    skipped = 0
    for r in rows:
        examined += 1
        # Quarantined rows are known-divergent — record and continue
        # without comparing previous_hash against the prior row, and
        # without advancing `prev` (the next row should chain back to
        # the last KNOWN-GOOD row, not to this divergent fork).
        if r["quarantined"]:
            skipped += 1
            continue
        if prev is None:
            prev = r
            continue
        if r["previous_hash"] != prev["hash"]:
            actual = (
                bytes(r["previous_hash"]).hex()
                if r["previous_hash"] is not None else None
            )
            expected = (
                bytes(prev["hash"]).hex()
                if prev["hash"] is not None else None
            )
            return ChainVerifyResult(
                rows_verified=examined,
                continuous=False,
                failure_reason=(
                    f"chain break at id={r['id']} "
                    f"created_at={r['created_at'].isoformat()}: "
                    f"previous_hash={actual} != prior.hash={expected}"
                ),
                first_break_id=r["id"],
                window_start=rows[0]["created_at"],
                window_end=rows[-1]["created_at"],
                quarantined_skipped=skipped,
            )
        prev = r

    return ChainVerifyResult(
        rows_verified=examined,
        continuous=True,
        failure_reason=None,
        first_break_id=None,
        window_start=rows[0]["created_at"],
        window_end=rows[-1]["created_at"],
        quarantined_skipped=skipped,
    )


__all__ = ["ChainVerifyResult", "verify_chain_window"]
