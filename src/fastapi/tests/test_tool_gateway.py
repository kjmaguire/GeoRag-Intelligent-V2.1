"""§4 Tool Gateway — integration tests.

Covers:
  - 19 tools registered in workspace.agent_risk_tiers
  - Unknown tool → blocked
  - R0 tool with no impl → blocked with explicit reason
  - R0 tool with impl + allowed workspace → executes
  - Workspace explicit deny → blocked
  - R3 tool emits audit_ledger row (action_type='tool.<name>')
  - R4 tool with missing credentials → blocked
  - R4 tool with valid credentials → allowed
  - dry_run=True → captured in dry_run_outputs, no impl call
  - Tool impl raising → outcome='error' but invocation row still written
"""
from __future__ import annotations

import os
import uuid
from uuid import UUID

import asyncpg
import pytest

from app.services.tool_gateway import (
    ToolGatewayContext,
    invoke_tool,
    register_tool,
)

PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)
TEST_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000001")

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN, statement_cache_size=0)
    try:
        yield conn
    finally:
        await conn.close()


# ─── Registry sanity ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_19_tools_registered_in_db(pg_conn: asyncpg.Connection):
    n = await pg_conn.fetchval(
        "SELECT count(*) FROM workspace.agent_risk_tiers",
    )
    assert n == 19, f"expected 19 tools, got {n}"


@pytest.mark.asyncio
async def test_tier_distribution(pg_conn: asyncpg.Connection):
    """Sanity check the R0-R5 distribution roughly matches the spec."""
    rows = await pg_conn.fetch(
        "SELECT risk_tier, count(*)::int AS n FROM workspace.agent_risk_tiers GROUP BY 1 ORDER BY 1",
    )
    dist = {r["risk_tier"]: r["n"] for r in rows}
    # At least some R0 (read-only) tools and some R4 (external publish) tools.
    assert dist.get("R0", 0) >= 4, dist  # read-onlys
    assert dist.get("R2", 0) >= 5, dist  # internal writes
    assert dist.get("R4", 0) >= 2, dist  # external publish + create_export


# ─── Gateway behaviour ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unknown_tool_blocked(pg_pool: asyncpg.Pool):
    name = f"not_a_real_tool_{uuid.uuid4().hex[:8]}"
    r = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="agent",
        ),
        tool_name=name,
        inputs={"x": 1},
    )
    assert r.allowed is False
    assert r.outcome == "blocked"
    assert "not registered" in (r.block_reason or "")


@pytest.mark.asyncio
async def test_r0_tool_no_impl_blocked(pg_pool: asyncpg.Pool):
    """audit_provenance is R0 but no impl bound here — should error."""
    r = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="agent",
        ),
        tool_name="audit_provenance",
        inputs={"silver_pk": "abc"},
    )
    assert r.outcome == "error"
    assert "no implementation" in (r.block_reason or "").lower()


@pytest.mark.asyncio
async def test_r0_tool_with_impl_executes(pg_pool: asyncpg.Pool):
    async def fake_impl(inputs):
        return {"echoed": inputs}
    register_tool("audit_provenance", fake_impl)

    r = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="agent",
        ),
        tool_name="audit_provenance",
        inputs={"silver_pk": "abc"},
    )
    assert r.allowed is True
    assert r.outcome == "allowed"
    assert r.output == {"echoed": {"silver_pk": "abc"}}
    assert r.duration_ms is not None and r.duration_ms >= 0


@pytest.mark.asyncio
async def test_workspace_explicit_deny_blocks(
    pg_pool: asyncpg.Pool, pg_conn: asyncpg.Connection,
):
    async def fake_impl(inputs):
        return {"ok": True}
    register_tool("query_public_geo", fake_impl)

    # Insert a deny rule
    await pg_conn.execute(
        "SELECT set_config('app.workspace_id', $1, false)", str(TEST_WORKSPACE_ID),
    )
    await pg_conn.execute(
        """
        INSERT INTO workspace.agent_permissions (workspace_id, tool_name, allowed, notes)
        VALUES ($1::uuid, 'query_public_geo', false, 'denied by test')
        ON CONFLICT (workspace_id, tool_name) DO UPDATE
            SET allowed = false, notes = 'denied by test'
        """,
        TEST_WORKSPACE_ID,
    )

    try:
        r = await invoke_tool(
            ctx=ToolGatewayContext(
                pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
                actor_user_id=999, actor_kind="agent",
            ),
            tool_name="query_public_geo",
            inputs={"q": "uranium"},
        )
        assert r.allowed is False
        assert r.outcome == "blocked"
        assert "denied by test" in (r.block_reason or "")
    finally:
        # cleanup
        await pg_conn.execute(
            "DELETE FROM workspace.agent_permissions "
            "WHERE workspace_id = $1::uuid AND tool_name = 'query_public_geo'",
            TEST_WORKSPACE_ID,
        )


@pytest.mark.asyncio
async def test_r3_tool_emits_audit_ledger(
    pg_pool: asyncpg.Pool, pg_conn: asyncpg.Connection,
):
    async def fake_impl(inputs):
        return {"notification_id": "ext-1234"}
    register_tool("request_approval", fake_impl)

    r = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="agent",
        ),
        tool_name="request_approval",
        inputs={"reason": "test"},
    )
    assert r.allowed is True
    assert r.outcome == "allowed"
    assert r.risk_tier == "R3"

    # Audit row should land
    audit = await pg_conn.fetchrow(
        """
        SELECT action_type FROM audit.audit_ledger
         WHERE action_type = 'tool.request_approval'
           AND target_id = $1
         LIMIT 1
        """,
        r.invocation_id,
    )
    assert audit is not None
    assert audit["action_type"] == "tool.request_approval"


@pytest.mark.asyncio
async def test_r4_tool_blocked_without_credentials(pg_pool: asyncpg.Pool):
    async def fake_impl(inputs):
        return {"published": True}
    register_tool("publish_arcgis", fake_impl)

    r = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="user",
            actor_metadata={},  # missing qp_credential_verified
        ),
        tool_name="publish_arcgis",
        inputs={"layer": "occurrences"},
    )
    assert r.allowed is False
    assert r.outcome == "blocked"
    assert "approval requirement" in (r.block_reason or "")


@pytest.mark.asyncio
async def test_r4_tool_allowed_with_credentials(pg_pool: asyncpg.Pool):
    async def fake_impl(inputs):
        return {"published": True}
    register_tool("publish_arcgis", fake_impl)

    r = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="user",
            actor_metadata={"qp_credential_verified": True},
        ),
        tool_name="publish_arcgis",
        inputs={"layer": "occurrences"},
    )
    assert r.allowed is True
    assert r.outcome == "allowed"
    assert r.output == {"published": True}


@pytest.mark.asyncio
async def test_dry_run_captures_no_execution(
    pg_pool: asyncpg.Pool, pg_conn: asyncpg.Connection,
):
    call_count = {"n": 0}
    async def fake_impl(inputs):
        call_count["n"] += 1
        return {"ok": True}
    register_tool("create_export", fake_impl)

    r = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="user",
            dry_run=True,
        ),
        tool_name="create_export",
        inputs={"format": "pdf"},
    )
    assert r.allowed is True
    assert r.outcome == "dry_run"
    assert r.dry_run_id is not None
    assert call_count["n"] == 0, "impl should NOT be called in dry_run"

    # Verify the dry_run_outputs row is there
    row = await pg_conn.fetchrow(
        "SELECT payload FROM workspace.dry_run_outputs WHERE id = $1::uuid",
        r.dry_run_id,
    )
    assert row is not None


@pytest.mark.asyncio
async def test_impl_exception_records_error_outcome(
    pg_pool: asyncpg.Pool, pg_conn: asyncpg.Connection,
):
    async def raising_impl(inputs):
        raise ValueError("synthetic failure")
    register_tool("validate_schema", raising_impl)

    r = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="agent",
        ),
        tool_name="validate_schema",
        inputs={"col": "foo"},
    )
    assert r.allowed is False
    assert r.outcome == "error"
    assert "synthetic failure" in (r.block_reason or "")

    # Invocation row should still be recorded
    row = await pg_conn.fetchrow(
        "SELECT outcome FROM workspace.tool_invocations WHERE invocation_id = $1::uuid",
        r.invocation_id,
    )
    assert row is not None
    assert row["outcome"] == "error"


@pytest.mark.asyncio
async def test_input_hash_stable(pg_pool: asyncpg.Pool, pg_conn: asyncpg.Connection):
    """Same inputs in different order → same hash."""
    async def fake_impl(inputs):
        return {"k": list(inputs.keys())}
    register_tool("retrieve_qdrant", fake_impl)

    r1 = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="agent",
        ),
        tool_name="retrieve_qdrant",
        inputs={"q": "test", "k": 10, "filters": {"a": 1, "b": 2}},
    )
    r2 = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pg_pool, workspace_id=TEST_WORKSPACE_ID,
            actor_user_id=999, actor_kind="agent",
        ),
        tool_name="retrieve_qdrant",
        inputs={"k": 10, "filters": {"b": 2, "a": 1}, "q": "test"},
    )
    h1 = await pg_conn.fetchval(
        "SELECT input_hash FROM workspace.tool_invocations WHERE invocation_id = $1::uuid",
        r1.invocation_id,
    )
    h2 = await pg_conn.fetchval(
        "SELECT input_hash FROM workspace.tool_invocations WHERE invocation_id = $1::uuid",
        r2.invocation_id,
    )
    assert h1 == h2, f"canonical hash should be stable across key order: {h1} != {h2}"
