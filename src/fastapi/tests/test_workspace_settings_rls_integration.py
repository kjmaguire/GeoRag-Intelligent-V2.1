"""RLS validation for silver.workspace_settings.

The Phase H4 migration enables FORCED row-level security on the table
with policy `workspace_settings_workspace_isolation`:

    USING       (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK  (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)

This test asserts the policy actually scopes reads/writes:

  - Within workspace A scope, A's row is visible, B's row is hidden.
  - Without an `app.workspace_id` GUC, no rows are visible.
  - Insert into A then attempt to insert the same workspace_id under
    B's scope must fail (WITH CHECK denies).
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_conn():
    """Connection that BYPASSES RLS — used for cleanup + workspace seeding."""
    conn = await asyncpg.connect(PG_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def app_conn(pg_conn: asyncpg.Connection):
    """Connection that ENFORCES RLS by switching to the non-bypass app role.

    The default `georag` connection role has BYPASSRLS — fine for ops/admin
    work but useless for testing tenant isolation. The FastAPI app connects
    as `georag_app` which is the role under which RLS applies. SET ROLE
    flips the active role for the rest of the session; RESET ROLE at the
    end of the test restores superuser for cleanup.
    """
    conn = await asyncpg.connect(PG_DSN)
    try:
        await conn.execute("SET ROLE georag_app")
        yield conn
    finally:
        try:
            await conn.execute("RESET ROLE")
        finally:
            await conn.close()


async def _ensure_workspace(conn: asyncpg.Connection, label: str) -> str:
    """Insert a workspace and return its UUID (cleanup is per-test)."""
    wid = str(uuid.uuid4())
    name = f"rls-test-{label}-{wid[:8]}"
    await conn.execute(
        """
        INSERT INTO silver.workspaces (workspace_id, name, slug, created_at)
        VALUES ($1::uuid, $2, $3, now())
        ON CONFLICT (workspace_id) DO NOTHING
        """,
        wid, name, name,
    )
    return wid


async def _set_scope(conn: asyncpg.Connection, workspace_id: str | None) -> None:
    if workspace_id is None:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
    else:
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", workspace_id)


async def _cleanup(conn: asyncpg.Connection, ws_ids: list[str]) -> None:
    # Drop scope so the privileged DELETEs land (we're georag, the table owner).
    # FORCED RLS applies to the table owner too — bypass via SET LOCAL ROLE if
    # needed, but the test connection IS the owner-superset so we use the
    # USING clause: set scope to each workspace and delete its own rows.
    for wid in ws_ids:
        await _set_scope(conn, wid)
        await conn.execute(
            "DELETE FROM silver.workspace_settings WHERE workspace_id = $1::uuid",
            wid,
        )
    await _set_scope(conn, None)
    for wid in ws_ids:
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", wid,
        )


@pytest.mark.asyncio
async def test_workspace_settings_rls_isolates_reads(
    pg_conn: asyncpg.Connection, app_conn: asyncpg.Connection,
) -> None:
    """Workspace A's scope reads A's row but NOT B's."""
    ws_a = await _ensure_workspace(pg_conn, "a")
    ws_b = await _ensure_workspace(pg_conn, "b")
    try:
        # Insert each row under its own scope (WITH CHECK requires it).
        await _set_scope(app_conn, ws_a)
        await app_conn.execute(
            "INSERT INTO silver.workspace_settings (workspace_id, default_tone) "
            "VALUES ($1::uuid, 'technical') "
            "ON CONFLICT (workspace_id) DO UPDATE SET default_tone = EXCLUDED.default_tone",
            ws_a,
        )
        await _set_scope(app_conn, ws_b)
        await app_conn.execute(
            "INSERT INTO silver.workspace_settings (workspace_id, default_tone) "
            "VALUES ($1::uuid, 'executive') "
            "ON CONFLICT (workspace_id) DO UPDATE SET default_tone = EXCLUDED.default_tone",
            ws_b,
        )

        # Scoped to A, only A's row is visible
        await _set_scope(app_conn, ws_a)
        a_rows = await app_conn.fetch(
            "SELECT workspace_id::text AS w, default_tone FROM silver.workspace_settings "
            "WHERE workspace_id IN ($1::uuid, $2::uuid)",
            ws_a, ws_b,
        )
        assert len(a_rows) == 1
        assert a_rows[0]["w"] == ws_a
        assert a_rows[0]["default_tone"] == "technical"

        # Scoped to B, only B's row is visible
        await _set_scope(app_conn, ws_b)
        b_rows = await app_conn.fetch(
            "SELECT workspace_id::text AS w, default_tone FROM silver.workspace_settings "
            "WHERE workspace_id IN ($1::uuid, $2::uuid)",
            ws_a, ws_b,
        )
        assert len(b_rows) == 1
        assert b_rows[0]["w"] == ws_b
        assert b_rows[0]["default_tone"] == "executive"
    finally:
        await _cleanup(pg_conn, [ws_a, ws_b])


@pytest.mark.asyncio
async def test_workspace_settings_rls_blocks_unscoped_reads(
    pg_conn: asyncpg.Connection, app_conn: asyncpg.Connection,
) -> None:
    """Without an app.workspace_id GUC, the table returns zero rows."""
    ws = await _ensure_workspace(pg_conn, "unscoped")
    try:
        await _set_scope(app_conn, ws)
        await app_conn.execute(
            "INSERT INTO silver.workspace_settings (workspace_id, default_tone) "
            "VALUES ($1::uuid, 'technical') "
            "ON CONFLICT (workspace_id) DO UPDATE SET default_tone = EXCLUDED.default_tone",
            ws,
        )
        # Clear scope
        await _set_scope(app_conn, None)
        rows = await app_conn.fetch(
            "SELECT workspace_id::text FROM silver.workspace_settings "
            "WHERE workspace_id = $1::uuid",
            ws,
        )
        assert len(rows) == 0, "row visible without app.workspace_id GUC set"
    finally:
        await _cleanup(pg_conn, [ws])


@pytest.mark.asyncio
async def test_workspace_settings_rls_blocks_cross_workspace_insert(
    pg_conn: asyncpg.Connection, app_conn: asyncpg.Connection,
) -> None:
    """Insert from workspace B's scope cannot write a row keyed on A.

    The WITH CHECK clause must reject the attempt with an integrity-error.
    """
    ws_a = await _ensure_workspace(pg_conn, "ins-a")
    ws_b = await _ensure_workspace(pg_conn, "ins-b")
    try:
        await _set_scope(app_conn, ws_b)
        with pytest.raises((asyncpg.exceptions.InsufficientPrivilegeError,
                            asyncpg.exceptions.CheckViolationError,
                            asyncpg.exceptions.IntegrityConstraintViolationError,
                            asyncpg.exceptions.NoDataFoundError)):
            await app_conn.execute(
                "INSERT INTO silver.workspace_settings (workspace_id, default_tone) "
                "VALUES ($1::uuid, 'technical')",
                ws_a,
            )
    finally:
        await _cleanup(pg_conn, [ws_a, ws_b])


@pytest.mark.asyncio
async def test_workspace_settings_rls_blocks_cross_workspace_update(
    pg_conn: asyncpg.Connection, app_conn: asyncpg.Connection,
) -> None:
    """A row created under A cannot be updated from B's scope."""
    ws_a = await _ensure_workspace(pg_conn, "upd-a")
    ws_b = await _ensure_workspace(pg_conn, "upd-b")
    try:
        await _set_scope(app_conn, ws_a)
        await app_conn.execute(
            "INSERT INTO silver.workspace_settings (workspace_id, default_tone) "
            "VALUES ($1::uuid, 'technical') "
            "ON CONFLICT (workspace_id) DO UPDATE SET default_tone = EXCLUDED.default_tone",
            ws_a,
        )
        # Now switch to B and attempt to update A's row.
        await _set_scope(app_conn, ws_b)
        result = await app_conn.execute(
            "UPDATE silver.workspace_settings SET default_tone = 'regulator' "
            "WHERE workspace_id = $1::uuid",
            ws_a,
        )
        # asyncpg returns "UPDATE 0" when RLS filtered the row out — no rows
        # were updated, which is the correct policy behaviour.
        assert result == "UPDATE 0", f"expected RLS to filter the row; got {result!r}"

        # Verify A's row is unchanged when we re-scope to A.
        await _set_scope(app_conn, ws_a)
        row = await app_conn.fetchrow(
            "SELECT default_tone FROM silver.workspace_settings "
            "WHERE workspace_id = $1::uuid",
            ws_a,
        )
        assert row is not None
        assert row["default_tone"] == "technical"
    finally:
        await _cleanup(pg_conn, [ws_a, ws_b])
