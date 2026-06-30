"""Live tests for `get_workspace_audit_excerpt` (doc-phase 121).

Exercises the workspace audit ledger excerpt aggregator against
real `record_decision` + `emit_support_access_audit` writes from
prior live helpers.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from app.audit.workspace_excerpt import (
    WorkspaceAuditExcerpt,
    get_workspace_audit_excerpt,
)
from app.services.decision_intelligence import record_decision
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
    email = f"audit-test-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1,$2,$3) RETURNING id",
        "Audit Excerpt User", email, "test-hash",
    )
    try:
        yield user_id
    finally:
        await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


@pytest.mark.asyncio
async def test_empty_workspace_excerpt(conn, synthetic_workspace):
    """Fresh workspace returns total=0 + empty entries + has_more=False."""
    excerpt = await get_workspace_audit_excerpt(
        conn, workspace_id=synthetic_workspace,
    )
    assert isinstance(excerpt, WorkspaceAuditExcerpt)
    assert excerpt.total_rows_in_window == 0
    assert excerpt.entries == []
    assert excerpt.has_more is False
    assert excerpt.page == 1


@pytest.mark.asyncio
async def test_excerpt_after_writes(
    conn, synthetic_workspace, synthetic_user
):
    """Decision + support_access writes appear in the excerpt newest-first."""
    decision_id = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="schema_mapping",
        recommendation="Map column 'au_g_t' → silver.assays.au_ppm",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
    )
    await emit_support_access_audit(
        conn,
        workspace_id=synthetic_workspace,
        ops_user_id=synthetic_user,
        ticket_id=None,
        access_kind="workspace_state_view",
        target_summary="Read workspace dashboard",
    )

    excerpt = await get_workspace_audit_excerpt(
        conn, workspace_id=synthetic_workspace,
    )
    assert excerpt.total_rows_in_window == 2
    assert len(excerpt.entries) == 2
    # Newest first
    action_types = [e.action_type for e in excerpt.entries]
    assert action_types[0] == "support_access"
    assert action_types[1] == "decision.schema_mapping"

    await conn.execute(
        "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
        str(decision_id),
    )


@pytest.mark.asyncio
async def test_action_type_filter(
    conn, synthetic_workspace, synthetic_user
):
    """action_type_filter narrows to substring match."""
    decision_id = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="export_approval",
        recommendation="Approve webhook",
        human_decision="rejected",
        decided_by_user_id=synthetic_user,
    )
    await emit_support_access_audit(
        conn,
        workspace_id=synthetic_workspace,
        ops_user_id=synthetic_user,
        ticket_id=None,
        access_kind="audit_ledger_excerpt",
        target_summary="Audit excerpt review",
    )

    # Filter for support_access only
    support_only = await get_workspace_audit_excerpt(
        conn,
        workspace_id=synthetic_workspace,
        action_type_filter="support_access",
    )
    assert support_only.total_rows_in_window == 1
    assert support_only.entries[0].action_type == "support_access"

    # Filter for decision.* only
    decision_only = await get_workspace_audit_excerpt(
        conn,
        workspace_id=synthetic_workspace,
        action_type_filter="decision.",
    )
    assert decision_only.total_rows_in_window == 1
    assert decision_only.entries[0].action_type == "decision.export_approval"

    await conn.execute(
        "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
        str(decision_id),
    )


@pytest.mark.asyncio
async def test_pagination(conn, synthetic_workspace, synthetic_user):
    """Multiple pages with correct has_more flag + offsets."""
    ids = []
    for i in range(5):
        d = await record_decision(
            conn,
            workspace_id=synthetic_workspace,
            decision_type="workflow_enablement",
            recommendation=f"Test decision {i}",
            human_decision="accepted",
            decided_by_user_id=synthetic_user,
        )
        ids.append(d)

    # Page 1: page_size=2 → 2 entries, has_more=True
    p1 = await get_workspace_audit_excerpt(
        conn, workspace_id=synthetic_workspace, page=1, page_size=2,
    )
    assert p1.total_rows_in_window == 5
    assert len(p1.entries) == 2
    assert p1.has_more is True

    # Page 3: should have 1 entry (5 - 2 - 2 = 1), has_more=False
    p3 = await get_workspace_audit_excerpt(
        conn, workspace_id=synthetic_workspace, page=3, page_size=2,
    )
    assert len(p3.entries) == 1
    assert p3.has_more is False

    # Page 4: beyond data → 0 entries, has_more=False
    p4 = await get_workspace_audit_excerpt(
        conn, workspace_id=synthetic_workspace, page=4, page_size=2,
    )
    assert len(p4.entries) == 0
    assert p4.has_more is False

    for d in ids:
        await conn.execute(
            "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
            str(d),
        )


@pytest.mark.asyncio
async def test_window_filter(conn, synthetic_workspace, synthetic_user):
    """Out-of-range window returns zero."""
    d = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="public_data_import",
        recommendation="Import BC MINFILE",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
    )

    past_start = datetime.now(UTC) - timedelta(days=10)
    past_end = past_start + timedelta(days=1)
    past_excerpt = await get_workspace_audit_excerpt(
        conn,
        workspace_id=synthetic_workspace,
        window_start=past_start,
        window_end=past_end,
    )
    assert past_excerpt.total_rows_in_window == 0

    await conn.execute(
        "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
        str(d),
    )


@pytest.mark.asyncio
async def test_invalid_window_raises(conn, synthetic_workspace):
    """end <= start raises ValueError."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="window_end .* must be > window_start"):
        await get_workspace_audit_excerpt(
            conn,
            workspace_id=synthetic_workspace,
            window_start=now,
            window_end=now,
        )


@pytest.mark.asyncio
async def test_invalid_page_raises(conn, synthetic_workspace):
    """page < 1 raises ValueError."""
    with pytest.raises(ValueError, match="page must be >= 1"):
        await get_workspace_audit_excerpt(
            conn, workspace_id=synthetic_workspace, page=0,
        )


@pytest.mark.asyncio
async def test_page_size_clamped(conn, synthetic_workspace):
    """page_size > 500 silently clamps to 500."""
    excerpt = await get_workspace_audit_excerpt(
        conn, workspace_id=synthetic_workspace, page_size=10_000,
    )
    assert excerpt.page_size == 500

    # And < 1 clamps up to 1
    excerpt2 = await get_workspace_audit_excerpt(
        conn, workspace_id=synthetic_workspace, page_size=0,
    )
    assert excerpt2.page_size == 1
