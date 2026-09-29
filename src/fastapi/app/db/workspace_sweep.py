"""Cross-workspace sweeps under a NOBYPASSRLS role (HAT-1, 2026-09-29).

Why this exists
---------------
Every Hatchet cron that walks "all the data" used to issue its discovery
query on a bare connection with ``app.workspace_id`` unset. That only works
for a role that bypasses RLS. In compose the worker connects as ``georag``
(superuser) so it always worked there. On AWS it connects as ``georag_app``
(``NOSUPERUSER NOBYPASSRLS``, deploy/aws/bootstrap.sql), and there an
unset GUC means:

* a **strict** policy (``workspace_id = NULLIF(current_setting(...), '')::uuid``)
  filters every row, so the sweep reads an empty table. It either does
  nothing, or it concludes from the empty read that the work is already
  done. ``silver.document_passages`` is strict after ``migrate`` +
  ``db:apply-raw``, which made the embed/enrich/verbalize fan-outs, the
  orphan-passage sweep, Tier 2's Qdrant spot-check and the stale sweep's
  "fully embedded?" test all read nothing;
* a **fail-open** policy (``... IS NULL OR workspace_id = ...``) happens to
  work, but only as long as nobody tightens it.

The fix Kyle chose (2026-09-29) is **not** an RLS-bypassing role. Each sweep
enumerates workspaces and runs its query once per workspace with the scope
bound. That holds under both policy shapes, and it does not widen what the
worker's role can see.

Enumeration
-----------
``silver.workspaces`` is readable with the GUC unset: its policy
(2026_05_20_010000) is deliberately ``GUC IS NULL OR workspace_id = GUC``,
so ``georag_app`` can list workspace ids without any new privilege. That was
verified on PostgreSQL 16 after ``migrate`` + ``db:apply-raw``, and
tests/test_cron_sweeps_under_app_role.py pins it. A SECURITY DEFINER
function would not have helped: ``FORCE ROW LEVEL SECURITY`` binds the
table owner too, so a definer function owned by a non-BYPASSRLS role sees
exactly what the caller sees.

If enumeration ever returns nothing, the helper logs
``WORKSPACE_ENUMERATION_EMPTY`` at ERROR. An empty list means every
per-workspace sweep does nothing, and that must not be silent.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

import asyncpg

from app.db.scoped_pool import UUID_RE, bind_workspace_scope

log = logging.getLogger(__name__)

#: Log marker for an enumeration that returned no workspaces. On a live
#: deployment there is always at least the default workspace, so an empty
#: list means the policy on silver.workspaces changed under the sweeps.
WORKSPACE_ENUMERATION_EMPTY_MARKER = "WORKSPACE_ENUMERATION_EMPTY"

LIST_WORKSPACES_SQL = (
    "SELECT workspace_id::text AS workspace_id "
    "FROM silver.workspaces ORDER BY workspace_id"
)


def affected_row_count(status: str) -> int:
    """Row count from an asyncpg command tag (``'UPDATE 3'`` gives 3), else 0."""
    tail = status.rsplit(" ", 1)[-1] if status else ""
    return int(tail) if tail.isdigit() else 0


def _require_no_open_transaction(conn: asyncpg.Connection, site: str) -> None:
    # The per-workspace scope is SET LOCAL inside a transaction this module
    # opens. Nested inside a caller's transaction that becomes a SAVEPOINT,
    # and a released savepoint keeps its SET LOCAL until the OUTER commit,
    # so the last workspace's scope would leak into the caller's next
    # statement. Refuse rather than leak.
    if conn.is_in_transaction():
        raise RuntimeError(
            f"workspace_sweep({site}): call on a connection with no open "
            f"transaction; each workspace gets its own transaction here."
        )


async def list_workspace_ids(
    conn: asyncpg.Connection, *, site: str = "unknown",
) -> list[str]:
    """Every workspace id, read with the scope explicitly cleared.

    Clearing it matters for a pooled connection that may carry a
    session-level scope from an earlier ``is_local=False`` bind: under the
    workspaces policy a bound scope would list just that one workspace.
    """
    _require_no_open_transaction(conn, site)
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.workspace_id', '', true)")
        rows = await conn.fetch(LIST_WORKSPACES_SQL)
    ids = [
        str(r["workspace_id"])
        for r in rows
        if r["workspace_id"] and UUID_RE.match(str(r["workspace_id"]))
    ]
    if not ids:
        log.error(
            "%s site=%s: silver.workspaces listed no workspaces for role %s "
            "with app.workspace_id unset; every per-workspace sweep will do "
            "nothing. Check the policy on silver.workspaces.",
            WORKSPACE_ENUMERATION_EMPTY_MARKER, site,
            await conn.fetchval("SELECT current_user"),
        )
    return ids


async def fetch_per_workspace(
    conn: asyncpg.Connection,
    sql: str,
    *args: Any,
    site: str,
    workspace_ids: Iterable[str] | None = None,
) -> list[asyncpg.Record]:
    """Run ``sql`` once per workspace with that workspace's scope bound.

    Returns the concatenated rows. Each workspace gets its own short
    transaction, so no scope outlives its own query and no transaction
    spans the whole sweep (the Hatchet queue shares this Postgres).

    ``sql`` must be a query whose result is meaningful per workspace:
    ``GROUP BY workspace_id`` and ``DISTINCT`` over rows that carry the
    workspace concatenate correctly; a bare ``count(*)`` gives one row per
    workspace for the caller to sum.
    """
    ids: Sequence[str] = (
        list(workspace_ids) if workspace_ids is not None
        else await list_workspace_ids(conn, site=site)
    )
    _require_no_open_transaction(conn, site)
    out: list[asyncpg.Record] = []
    for workspace_id in ids:
        async with conn.transaction():
            await bind_workspace_scope(conn, workspace_id=workspace_id, site=site)
            out.extend(await conn.fetch(sql, *args))
    return out


async def execute_per_workspace(
    conn: asyncpg.Connection,
    sql: str,
    *args: Any,
    site: str,
    workspace_ids: Iterable[str] | None = None,
) -> list[str]:
    """``conn.execute(sql)`` once per workspace; returns the status strings."""
    ids: Sequence[str] = (
        list(workspace_ids) if workspace_ids is not None
        else await list_workspace_ids(conn, site=site)
    )
    _require_no_open_transaction(conn, site)
    statuses: list[str] = []
    for workspace_id in ids:
        async with conn.transaction():
            await bind_workspace_scope(conn, workspace_id=workspace_id, site=site)
            statuses.append(await conn.execute(sql, *args))
    return statuses


__all__ = [
    "LIST_WORKSPACES_SQL",
    "affected_row_count",
    "WORKSPACE_ENUMERATION_EMPTY_MARKER",
    "execute_per_workspace",
    "fetch_per_workspace",
    "list_workspace_ids",
]
