"""Live tests for `open_trace_with_audit` + `build_langfuse_trace_url`
(doc-phase 118).

Verifies the §10.13 LangFuse trace replay link integration:
- Pure URL builder works on its own
- open_trace_with_audit combines URL build + audit emission
- audit_ledger row carries access_kind='langfuse_trace_read'
- trace_id validation rejects empty
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from app.services.support_cockpit import (
    build_langfuse_trace_url,
    open_trace_with_audit,
)


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
    await conn.execute("SELECT set_config('app.workspace_id', $1, false)", str(ws_id))
    try:
        yield ws_id
    finally:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", str(ws_id)
        )


@pytest.fixture
async def synthetic_user(conn):
    email = f"langfuse-test-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1,$2,$3) RETURNING id",
        "LangFuse Test User", email, "test-hash",
    )
    try:
        yield user_id
    finally:
        await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


def test_url_builder_with_explicit_base_url():
    """Pure function — no DB needed."""
    url = build_langfuse_trace_url("abc123", base_url="https://langfuse.example.com")
    assert url == "https://langfuse.example.com/trace/abc123"


def test_url_builder_strips_trailing_slash():
    """Trailing slash on base_url is normalized."""
    url = build_langfuse_trace_url("abc", base_url="https://langfuse.example.com/")
    assert url == "https://langfuse.example.com/trace/abc"


def test_url_builder_env_default(monkeypatch):
    """LANGFUSE_BASE_URL env var honored when no explicit base_url."""
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.test")
    url = build_langfuse_trace_url("xyz")
    assert url == "https://lf.test/trace/xyz"


@pytest.mark.asyncio
async def test_open_trace_with_audit_emits_audit_row(
    conn, synthetic_workspace, synthetic_user
):
    """Happy path: returns URL + audit_ledger_id + writes audit row."""
    trace_id = f"trace-{uuid4()}"
    result = await open_trace_with_audit(
        conn,
        trace_id=trace_id,
        workspace_id=synthetic_workspace,
        ops_user_id=synthetic_user,
        base_url="https://langfuse.example.com",
    )

    assert result["trace_id"] == trace_id
    assert result["url"] == f"https://langfuse.example.com/trace/{trace_id}"
    assert "audit_ledger_id" in result

    # Audit row landed
    row = await conn.fetchrow(
        "SELECT action_type, payload, actor_id FROM audit.audit_ledger "
        "WHERE id = $1::uuid",
        str(result["audit_ledger_id"]),
    )
    assert row["action_type"] == "support_access"
    assert row["actor_id"] == synthetic_user

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["access_kind"] == "langfuse_trace_read"
    assert payload["trace_id"] == trace_id
    assert payload["url"] == f"https://langfuse.example.com/trace/{trace_id}"


@pytest.mark.asyncio
async def test_open_trace_with_ticket_id_threads_through(
    conn, synthetic_workspace, synthetic_user
):
    """ticket_id is plumbed into the audit row target_id."""
    fake_ticket_id = uuid4()
    trace_id = f"trace-{uuid4()}"

    result = await open_trace_with_audit(
        conn,
        trace_id=trace_id,
        workspace_id=synthetic_workspace,
        ops_user_id=synthetic_user,
        ticket_id=fake_ticket_id,
    )

    row = await conn.fetchrow(
        "SELECT target_schema, target_table, target_id FROM audit.audit_ledger "
        "WHERE id = $1::uuid",
        str(result["audit_ledger_id"]),
    )
    assert row["target_schema"] == "ops"
    assert row["target_table"] == "support_tickets"
    assert row["target_id"] == str(fake_ticket_id)


@pytest.mark.asyncio
async def test_empty_trace_id_raises(conn, synthetic_workspace, synthetic_user):
    """Empty / whitespace trace_id raises ValueError."""
    with pytest.raises(ValueError, match="trace_id is required"):
        await open_trace_with_audit(
            conn,
            trace_id="",
            workspace_id=synthetic_workspace,
            ops_user_id=synthetic_user,
        )

    with pytest.raises(ValueError, match="trace_id is required"):
        await open_trace_with_audit(
            conn,
            trace_id="   ",
            workspace_id=synthetic_workspace,
            ops_user_id=synthetic_user,
        )
