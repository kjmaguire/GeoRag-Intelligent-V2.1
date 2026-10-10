"""How a §25.4 support agent gets a connection scoped to its ticket's workspace.

Two ways in, one result: ``(conn, ticket_row)`` inside a transaction whose
``app.workspace_id`` is the ticket's workspace.

* ``workspace_id`` given -- the caller has ALREADY authorised that workspace
  (``POST /internal/v1/workflows/support_replay/trigger`` checks the ticket
  lives in it before it dispatches). The connection is scoped straight to it
  and the ticket is read under that scope. Nothing is discovered under another
  tenant.
* ``workspace_id`` omitted -- the older ADR-0014 route: bind the legacy default
  tenant, read the ticket to find its workspace, rebind. That read is the
  problem. ``ops.support_*`` carries a STRICT policy
  (``workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid``,
  database/raw/phase0/98-rls-tenant-isolation-block3.sql) with no fail-open
  branch, so as ``georag_app`` (NOBYPASSRLS, the AWS worker's role) a ticket in
  any workspace but the default one is invisible to the lookup and the agent
  dies with ``BareConnectionError: lookup_sql returned no rows`` -- after the
  trigger had already confirmed the ticket exists. Compose connects as a
  superuser and never showed it. Callers that know the workspace must say so.

``read_only`` turns the transaction READ ONLY at the database. It is the dry-run
guarantee: a code path that forgot to skip a write fails with "cannot execute
INSERT in a read-only transaction" instead of mutating a live ticket.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from uuid import UUID

import asyncpg

from app.db import BareConnectionError, lookup_and_rescope, scoped_connection


def normalise_workspace(workspace_id: UUID | str | None) -> str | None:
    """``workspace_id`` as the lower-case string the helpers compare, or None."""
    return None if workspace_id is None else str(workspace_id).lower()


@contextlib.asynccontextmanager
async def ticket_connection(
    pool: asyncpg.Pool,
    *,
    ticket_id: str,
    lookup_sql: str,
    site: str,
    bootstrap_reason: str,
    workspace_id: UUID | str | None = None,
    read_only: bool = False,
) -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Record]]:
    """Yield ``(conn, ticket_row)`` scoped to the ticket's workspace.

    ``lookup_sql`` takes the ticket id as ``$1`` and must select
    ``workspace_id``. A ticket that is not visible in ``workspace_id`` raises
    ``BareConnectionError("... lookup_sql returned no rows ...")``, the same
    error ``lookup_and_rescope`` raises for a missing ticket, so every caller's
    existing "not found" translation keeps working.
    """
    scope = normalise_workspace(workspace_id)
    if scope is None:
        async with lookup_and_rescope(
            pool,
            lookup_sql=lookup_sql,
            lookup_args=(ticket_id,),
            site=site,
            bootstrap_reason=bootstrap_reason,
        ) as (conn, row):
            if read_only:
                await conn.execute("SET TRANSACTION READ ONLY")
            yield conn, row
        return

    async with scoped_connection(pool, workspace_id=scope, site=site) as conn:
        if read_only:
            await conn.execute("SET TRANSACTION READ ONLY")
        row = await conn.fetchrow(lookup_sql, ticket_id)
        # Under RLS a ticket from another workspace is simply not returned; a
        # role that bypasses RLS would return it, and operating on it under
        # the wrong scope is exactly what this helper exists to prevent.
        if row is None or str(row["workspace_id"]).lower() != scope:
            raise BareConnectionError(
                f"ticket_connection({site}): lookup_sql returned no rows for "
                f"ticket {ticket_id} in workspace {scope}"
            )
        yield conn, row


__all__ = ["normalise_workspace", "ticket_connection"]
