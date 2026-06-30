"""Live tests for the doc-phase 148 restore_workspace graduation
(dry-run consistency-check path)."""
from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.hatchet_workflows.restore_workspace import (
    RestoreWorkspaceInput,
)
from app.hatchet_workflows.restore_workspace import (
    execute as restore_workspace_execute,
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


@pytest.mark.asyncio
async def test_restore_workspace_dry_run_against_default_workspace(conn):
    """Default Workspace has known seeded state — verify the counts surface."""
    default_ws = UUID("a0000000-0000-0000-0000-000000000001")
    inp = RestoreWorkspaceInput(
        workspace_id=default_ws,
        snapshot_manifest_uri="s3://georag-backups/manifests/synthetic.json",
        initiated_by_user_id=971,
        restore_request_id=uuid4(),
        dry_run=True,
    )
    out = await restore_workspace_execute.aio_mock_run(inp)
    assert out.success is True
    assert out.stores_restored == []  # dry run
    # Phase G.2 expanded the shape from a flat `row_counts` dict to a
    # nested `live_counts` block carrying per-store counts (postgres /
    # neo4j / qdrant / redis). The legacy Postgres-only assertions still
    # apply under `live_counts.postgres`.
    rc = out.consistency_check_results["live_counts"]["postgres"]
    assert rc["silver_workspaces"] == 1
    assert rc["silver_hypotheses"] >= 9   # doc-phase 134 seeded 9
    assert rc["ops_support_tickets"] >= 6  # doc-phase 136 seeded 6
    assert rc["audit_ledger_anchors"] > 100  # 170+ from prior ticks
    assert out.audit_ledger_entry_id is not None


@pytest.mark.asyncio
async def test_restore_workspace_unknown_workspace_returns_failure():
    inp = RestoreWorkspaceInput(
        workspace_id=uuid4(),  # nonexistent
        snapshot_manifest_uri="s3://nope.json",
        initiated_by_user_id=971,
        restore_request_id=uuid4(),
        dry_run=True,
    )
    out = await restore_workspace_execute.aio_mock_run(inp)
    assert out.success is False
    assert out.failure_stage == "workspace_lookup"
    assert "not found" in (out.failure_reason or "")


@pytest.mark.asyncio
async def test_restore_workspace_real_mode_is_explicitly_gated():
    """`dry_run=False` must return failure with explicit message —
    never silently silently destructive."""
    inp = RestoreWorkspaceInput(
        workspace_id=UUID("a0000000-0000-0000-0000-000000000001"),
        snapshot_manifest_uri="s3://nope.json",
        initiated_by_user_id=971,
        restore_request_id=uuid4(),
        dry_run=False,
    )
    out = await restore_workspace_execute.aio_mock_run(inp)
    assert out.success is False
    assert out.failure_stage == "precheck"
    assert "backup infrastructure" in (out.failure_reason or "")


@pytest.mark.asyncio
async def test_restore_workspace_emits_audit_anchor(conn):
    """Verify the workspace_restore audit anchor lands."""
    request_id = uuid4()
    inp = RestoreWorkspaceInput(
        workspace_id=UUID("a0000000-0000-0000-0000-000000000001"),
        snapshot_manifest_uri="s3://georag-backups/synthetic.json",
        initiated_by_user_id=971,
        restore_request_id=request_id,
        dry_run=True,
    )
    out = await restore_workspace_execute.aio_mock_run(inp)
    assert out.audit_ledger_entry_id is not None

    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'workspace_restore'
           AND id = $1::uuid
        """,
        str(out.audit_ledger_entry_id),
    )
    assert n == 1
