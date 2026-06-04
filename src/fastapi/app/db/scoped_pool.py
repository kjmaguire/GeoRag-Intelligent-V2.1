"""Canonical workspace-scoped asyncpg acquisition (REC#2, 2026-06-03).

Background
----------
The codebase has 20 production files that each do their own:

    async with pg_pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", wsid)
        ... real query ...

This is the SAME shape repeated 20 times, and every repetition is an
opportunity to:
  - forget the call entirely (silent RLS bypass — fail-open under
    canonical policies)
  - use the legacy `georag.workspace_id` GUC name (fail-open in a
    different way — three audits caught instances of this)
  - use `false` instead of `true` for SET LOCAL scope (leaks GUC
    across pooled connection re-use)
  - skip the surrounding transaction (SET LOCAL needs a tx to scope)
  - skip parameter binding + interpolate the UUID via f-string
    (Theme G shadow-trigger injection class)

Architectural fix
-----------------
ONE canonical async context manager that does all of the above
correctly + raises if asked to scope to an invalid UUID. Every call
site uses this; we delete the per-site GUC-set boilerplate. The 12
remaining legacy-GUC writers (per
project_legacy_guc_writers_audit_2026_05_28.md) migrate to this
helper and lose their hand-rolled GUC code.

Phase 2 of the rollout drops the `georag.workspace_id` GUC name from
all RLS policies — once no production code sets it, the legacy
fail-open variant becomes impossible.

Usage
-----

Direct (no AgentDeps):

    from app.db import scoped_connection

    async with scoped_connection(pool, workspace_id=ws.workspace_id) as conn:
        rows = await conn.fetch("SELECT ... FROM silver.foo WHERE ...")

Inside an agent tool (AgentDeps has its own ``acquire_scoped`` that
threads ``project_id`` too):

    async with deps.acquire_scoped() as conn:
        ...

The helper here is for the call sites that DON'T have an AgentDeps
context — hatchet workflows, support_cockpit services, phase10 agents,
routers using request.state.

Pinned by tests/test_scoped_connection.py.
"""
from __future__ import annotations

import contextlib
import logging
import re
from typing import AsyncIterator

import asyncpg


log = logging.getLogger(__name__)


# UUID v4-ish — same shape used in app/agent/deps.py. Anything that
# doesn't match is refused before being interpolated into SET LOCAL.
# Centralised here so the regex doesn't drift across files (every drift
# audit found at least one variant).
UUID_RE: re.Pattern[str] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class BareConnectionError(RuntimeError):
    """Raised when scoped_connection refuses to scope to a missing/invalid workspace_id.

    The intent mirrors REC#1: silently scoping a write to the default
    tenant is the bug class we're closing, so the failure is loud +
    typed + carries enough context for ops to trace the call site.
    """


async def bind_workspace_scope(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    site: str = "unknown",
) -> None:
    """Bind ``app.workspace_id`` on an already-acquired connection.

    Use this when a single long-lived connection iterates over multiple
    workspaces — eg. nightly cron jobs in
    ``hatchet_workflows/continuous_learning_loop.py`` that walk
    every active workspace, or ``services/ingest/passage_embedder.py``
    where the workspace is fixed for the function but the connection
    is passed in.

    For one-shot workspace-scoped operations that own their own
    connection, use :func:`scoped_connection` instead — that one
    handles acquire + transaction + GUC + yield in a single context
    manager.

    Same validation as ``scoped_connection``: refuses missing /
    empty / non-UUID workspace_id with ``BareConnectionError``. Same
    parameter-bound set_config form so f-string injection is
    impossible. Same Theme G-compliant ``true`` scope so the GUC
    survives across statements in the surrounding transaction (and
    does NOT leak across transactions under PgBouncer transaction-pool
    mode, which is the safety property we're after).

    Does NOT open a transaction — caller is responsible for that.
    Loop callers typically already have one transaction wrapping the
    entire iteration; opening a per-rebind transaction would defeat
    the batching that's the whole reason for the loop pattern.
    """
    if workspace_id is None or str(workspace_id).strip() == "":
        raise BareConnectionError(
            f"bind_workspace_scope({site}): workspace_id is required + non-empty. "
            f"For legitimate default-tenant work, use "
            f"app.hatchet_workflows._workspace_input.bootstrap_workspace_id "
            f"explicitly."
        )
    wid = str(workspace_id).strip()
    if not UUID_RE.match(wid):
        raise BareConnectionError(
            f"bind_workspace_scope({site}): refusing to bind non-UUID "
            f"workspace_id={wid!r}. SQL injection class (Theme G)."
        )
    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, true)",
        wid,
    )


@contextlib.asynccontextmanager
async def lookup_and_rescope(
    pool: asyncpg.Pool,
    *,
    lookup_sql: str,
    lookup_args: tuple,
    site: str,
    bootstrap_reason: str,
    workspace_col: str = "workspace_id",
):
    """Two-phase workspace scoping: bootstrap → lookup → pivot.

    See ADR-0014 for the full rationale. Use this for support-context
    workflows that must read a row to discover its workspace before
    scoping subsequent writes (currently: the 5
    ``services/support_cockpit/`` agents + ``hatchet_workflows/
    support_replay.py``).

    Sequence
    --------
    1. Acquire a pool connection + open a transaction.
    2. Bind ``app.workspace_id`` to the legacy default tenant via
       :func:`bootstrap_workspace_id` so the cross-tenant lookup
       succeeds. The bootstrap is COUNTED through
       ``WORKSPACE_RESOLUTION_FAILURES`` so ops sees elevation rate.
    3. Run ``lookup_sql`` with ``lookup_args``. Must return at least
       one row.
    4. Extract ``row[workspace_col]``, UUID-validate it, REBIND
       ``app.workspace_id`` to that value via
       :func:`bind_workspace_scope`.
    5. Yield ``(conn, row)`` to the caller. Subsequent reads/writes
       on ``conn`` are scoped to the looked-up workspace.

    Args:
        pool: asyncpg.Pool — typically ``request.app.state.pg_pool``.
        lookup_sql: A parameterized SELECT that returns AT LEAST the
            workspace_id column. Common shape:
            ``"SELECT workspace_id, category FROM ops.support_tickets
              WHERE ticket_id = $1::uuid"``.
        lookup_args: Tuple of positional arguments for ``lookup_sql``.
        site: Short tag for the call site. Used for log + error
            context.
        bootstrap_reason: REQUIRED — must be on
            ``ALLOWED_BOOTSTRAP_REASONS``. Pins the legitimate
            cross-tenant elevation point so an attacker / contributor
            can't sneak in a 7th elevation path without a code change
            + review.
        workspace_col: Name of the column in the lookup result that
            holds the pivot value. Defaults to ``workspace_id``.

    Raises:
        BareConnectionError: when the lookup returns no rows, the
            ``workspace_col`` value is None/empty, or the value
            doesn't match the UUID shape.
        ValueError: when ``bootstrap_reason`` isn't on the allowlist
            (raised by ``bootstrap_workspace_id`` itself).
    """
    # Import here to avoid a top-level cycle (workspace_input imports
    # metrics which imports settings which imports prometheus_client).
    from app.hatchet_workflows._workspace_input import bootstrap_workspace_id  # noqa: PLC0415

    bootstrap_uuid = bootstrap_workspace_id(reason=bootstrap_reason)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Phase 1: bind to the legitimate cross-tenant bootstrap UUID.
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)",
                bootstrap_uuid,
            )
            # Phase 2: cross-tenant lookup.
            row = await conn.fetchrow(lookup_sql, *lookup_args)
            if row is None:
                raise BareConnectionError(
                    f"lookup_and_rescope({site}): lookup_sql returned no "
                    f"rows. args={lookup_args!r}. The caller is responsible "
                    f"for either using a SQL with explicit existence "
                    f"checking, or catching this exception."
                )
            # Phase 3: extract + validate the pivot value.
            pivot_value = row[workspace_col] if workspace_col in row else None
            if pivot_value is None or str(pivot_value).strip() == "":
                raise BareConnectionError(
                    f"lookup_and_rescope({site}): lookup row has no "
                    f"non-empty {workspace_col!r} column. row.keys()="
                    f"{list(row.keys()) if hasattr(row, 'keys') else row!r}"
                )
            # Phase 4: rebind to the looked-up workspace. UUID validation
            # is done by bind_workspace_scope — corrupt data fails loud
            # here instead of injecting into SET LOCAL.
            await bind_workspace_scope(
                conn,
                workspace_id=str(pivot_value),
                site=f"{site}.pivot",
            )
            # Phase 5: yield to caller.
            yield conn, row


@contextlib.asynccontextmanager
async def scoped_connection(
    pool: asyncpg.Pool,
    *,
    workspace_id: str,
    site: str = "unknown",
    statement_timeout_ms: int | None = None,
) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a workspace-scoped asyncpg connection.

    What this does (in order):
      1. Validates ``workspace_id`` matches the UUID shape — refuses
         empty, None, or anything else with ``BareConnectionError``.
      2. Acquires a connection from the pool.
      3. Opens a transaction (required by SET LOCAL semantics + PgBouncer
         transaction-pool mode).
      4. Sets ``app.workspace_id`` to the given value, parameter-bound
         (NOT f-string — Theme G fix).
      5. Optionally sets ``statement_timeout`` to bound runaway queries.
      6. Yields the connection.

    What this does NOT do:
      - Set the legacy ``georag.workspace_id`` GUC. That name is in
        active deprecation; new code must not touch it. Phase-2 of
        REC#2 drops it from RLS policies entirely.
      - Set ``app.project_id`` (that's project scoping, handled by
        ``AgentDeps.acquire_scoped`` separately). Workspace scoping is
        the strict superset that REC#2 covers.
      - Catch DB errors during the SET. A failure to set the GUC is a
        hard fail — better to surface it than execute the body without
        the scope.

    Args:
        pool: asyncpg.Pool — typically ``request.app.state.pg_pool``
            (FastAPI) or a workflow-scoped pool (Hatchet).
        workspace_id: Non-empty UUID string. Bootstrap callers use
            ``app.hatchet_workflows._workspace_input.bootstrap_workspace_id``
            to get the legacy default tenant explicitly + logged.
        site: Short tag identifying the call site for log/error context.
            Mirrors the ``site`` label used by
            ``WORKSPACE_RESOLUTION_FAILURES`` so the two layers
            attribute consistently.
        statement_timeout_ms: When set, applies ``SET LOCAL
            statement_timeout`` so a wedged query doesn't camp the
            connection forever. Defaults to None (no extra timeout).

    Raises:
        BareConnectionError: workspace_id is missing or doesn't match
            the UUID regex.
    """
    if workspace_id is None or str(workspace_id).strip() == "":
        raise BareConnectionError(
            f"scoped_connection({site}): workspace_id is required + non-empty. "
            f"If this call site genuinely needs to scope to the default tenant, "
            f"use app.hatchet_workflows._workspace_input.bootstrap_workspace_id "
            f"explicitly — silent default fallback is exactly the bug class "
            f"REC#1+REC#2 close."
        )
    wid = str(workspace_id).strip()
    if not UUID_RE.match(wid):
        raise BareConnectionError(
            f"scoped_connection({site}): refusing to scope to non-UUID "
            f"workspace_id={wid!r}. The UUID is interpolated into "
            f"SET LOCAL (asyncpg can't parameterise GUC names), so any "
            f"value here that escapes the UUID shape is a SQL injection "
            f"vector — see Theme G fix on shadow_trigger.py."
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            if statement_timeout_ms is not None:
                await conn.execute(
                    f"SET LOCAL statement_timeout = '{int(statement_timeout_ms)}ms'"
                )
            # SET LOCAL with parameter binding via set_config — the
            # 3rd arg `true` scopes to the surrounding transaction
            # so PgBouncer transaction-pool mode is safe. Theme G
            # made this the canonical form; we adopt it here too.
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)",
                wid,
            )
            yield conn
