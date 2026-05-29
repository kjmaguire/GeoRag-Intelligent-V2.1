"""Live tests for `emit_support_access_audit` (doc-phase 116).

Verifies the §10.12 / §25.3 support access logger:
- Emits audit_ledger row with action_type='support_access'
- Captures access_kind + target_summary + ticket_id in payload
- Chains correctly into the existing hash chain
- Validates target_summary is non-empty
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from app.services.support_cockpit import emit_support_access_audit


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_workspace(conn):
    ws_id = uuid4()
    await conn.execute(
        "INSERT INTO silver.workspaces (workspace_id, name, slug) "
        "VALUES ($1::uuid, $2, $3)",
        str(ws_id), f"test-ws-{ws_id}", f"test-ws-{ws_id}",
    )
    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, false)", str(ws_id)
    )
    try:
        yield ws_id
    finally:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid",
            str(ws_id),
        )


@pytest.fixture
async def synthetic_user(conn):
    email = f"ops-test-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1,$2,$3) RETURNING id",
        "Ops Test User", email, "test-hash",
    )
    try:
        yield user_id
    finally:
        await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


@pytest.mark.asyncio
async def test_emit_basic_workspace_state_view(conn, synthetic_workspace, synthetic_user):
    """Minimal access audit: workspace_state_view without a ticket."""
    entry = await emit_support_access_audit(
        conn,
        workspace_id=synthetic_workspace,
        ops_user_id=synthetic_user,
        ticket_id=None,
        access_kind="workspace_state_view",
        target_summary="Read workspace dashboard state",
    )

    assert entry.action_type == "support_access"
    assert entry.workspace_id == synthetic_workspace
    assert entry.hash is not None
    assert len(entry.hash) == 32

    # Confirm the row landed in audit.audit_ledger
    row = await conn.fetchrow(
        "SELECT action_type, actor_kind, actor_id, target_schema, target_table, "
        "target_id, payload "
        "FROM audit.audit_ledger WHERE id = $1::uuid",
        str(entry.id),
    )
    assert row["action_type"] == "support_access"
    assert row["actor_kind"] == "user"
    assert row["actor_id"] == synthetic_user
    assert row["target_schema"] == "ops"
    assert row["target_table"] is None  # no ticket_id → no target_table
    assert row["target_id"] is None

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["access_kind"] == "workspace_state_view"
    assert payload["target_summary"] == "Read workspace dashboard state"
    assert "ticket_id" not in payload


@pytest.mark.asyncio
async def test_emit_with_ticket_id_populates_target(
    conn, synthetic_workspace, synthetic_user
):
    """When a ticket_id is supplied, target_table + target_id are populated."""
    fake_ticket_id = uuid4()
    entry = await emit_support_access_audit(
        conn,
        workspace_id=synthetic_workspace,
        ops_user_id=synthetic_user,
        ticket_id=fake_ticket_id,
        access_kind="workflow_replay_dry_run",
        target_summary="Replay ingest_pdf workflow run 42",
    )

    row = await conn.fetchrow(
        "SELECT target_schema, target_table, target_id, payload "
        "FROM audit.audit_ledger WHERE id = $1::uuid",
        str(entry.id),
    )
    assert row["target_schema"] == "ops"
    assert row["target_table"] == "support_tickets"
    assert row["target_id"] == str(fake_ticket_id)

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["access_kind"] == "workflow_replay_dry_run"
    assert payload["ticket_id"] == str(fake_ticket_id)


@pytest.mark.asyncio
async def test_emit_with_extra_payload_merges(
    conn, synthetic_workspace, synthetic_user
):
    """Caller payload merges with internal payload; internal keys win on conflict."""
    entry = await emit_support_access_audit(
        conn,
        workspace_id=synthetic_workspace,
        ops_user_id=synthetic_user,
        ticket_id=None,
        access_kind="audit_ledger_excerpt",
        target_summary="Audit excerpt 2026-05-01 → 2026-05-13",
        payload={
            "window_start": "2026-05-01T00:00:00Z",
            "window_end": "2026-05-13T00:00:00Z",
            "row_count": 1247,
            # Try to override an internal key — should NOT win.
            "access_kind": "spoofed-attempt",
        },
    )

    row = await conn.fetchrow(
        "SELECT payload FROM audit.audit_ledger WHERE id = $1::uuid",
        str(entry.id),
    )
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)

    # Caller's window keys land.
    assert payload["window_start"] == "2026-05-01T00:00:00Z"
    assert payload["row_count"] == 1247
    # Internal access_kind wins over caller's spoof attempt.
    assert payload["access_kind"] == "audit_ledger_excerpt"


@pytest.mark.asyncio
async def test_empty_target_summary_raises(conn, synthetic_workspace, synthetic_user):
    """Empty target_summary raises ValueError before any DB write."""
    with pytest.raises(ValueError, match="target_summary is required"):
        await emit_support_access_audit(
            conn,
            workspace_id=synthetic_workspace,
            ops_user_id=synthetic_user,
            ticket_id=None,
            access_kind="workspace_state_view",
            target_summary="",
        )

    with pytest.raises(ValueError, match="target_summary is required"):
        await emit_support_access_audit(
            conn,
            workspace_id=synthetic_workspace,
            ops_user_id=synthetic_user,
            ticket_id=None,
            access_kind="workspace_state_view",
            target_summary="   ",
        )
