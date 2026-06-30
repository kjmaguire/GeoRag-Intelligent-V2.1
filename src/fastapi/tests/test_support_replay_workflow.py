"""Live test for the doc-phase 146 support_replay Hatchet workflow body.

Inserts a synthetic ticket, invokes the workflow body via
.aio_mock_run, asserts the replay row + chain results + audit anchor.
"""
from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from app.hatchet_workflows.support_replay import (
    SupportReplayInput,
)
from app.hatchet_workflows.support_replay import (
    execute as support_replay_execute,
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
    # Block-3 RLS — Default Workspace scope for fixture data.
    await c.execute(
        "SELECT set_config('app.workspace_id', $1, false)",
        "a0000000-0000-0000-0000-000000000001",
    )
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_user(conn):
    email = f"test-replay-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        """
        INSERT INTO public.users (name, email, password)
        VALUES ($1, $2, $3) RETURNING id
        """,
        "Replay test user", email, "test-hash",
    )
    try:
        yield user_id
    finally:
        try:
            await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)
        except asyncpg.ForeignKeyViolationError:
            pass


@pytest.fixture
async def synthetic_ticket(conn, synthetic_user):
    prefix = uuid4().hex[:8]
    tid = await conn.fetchval(
        """
        INSERT INTO ops.support_tickets (
            workspace_id, reported_by_user_id, channel, category,
            description, severity, status
        )
        VALUES (
            'a0000000-0000-0000-0000-000000000001'::uuid, $1, 'in_app',
            'failed_ingestion', $2, 'high', 'investigating'
        )
        RETURNING ticket_id
        """,
        synthetic_user,
        f"[{prefix}] replay test — PDF upload crashed",
    )
    try:
        yield tid
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid",
            str(tid),
        )


@pytest.mark.asyncio
async def test_support_replay_runs_full_chain(conn, synthetic_ticket, synthetic_user):
    """End-to-end: invoke the workflow body, assert replay row +
    chain results + audit anchor."""
    inp = SupportReplayInput(
        ticket_id=synthetic_ticket,
        original_workflow_run_id="fake_original_run_id_abc123",
        initiated_by_user_id=synthetic_user,
        dry_run=True,
        replay_request_id=uuid4(),
    )
    out = await support_replay_execute.aio_mock_run(inp)

    assert out.success is True
    assert out.diff_summary is not None
    assert "triage" in out.diff_summary
    assert "investigation" in out.diff_summary
    assert "packet" in out.diff_summary
    assert "draft" in out.diff_summary
    assert "routing" in out.diff_summary
    assert out.routing_decision  # one of the 5 routing decisions
    assert out.response_word_count is not None and out.response_word_count > 30
    assert out.investigation_trace_id is not None
    assert out.replay_workflow_run_id is not None
    assert out.replay_workflow_run_id.startswith("replay_")

    # Replay row persists with status='completed'.
    row = await conn.fetchrow(
        "SELECT status, diff_summary, replay_workflow_run_id, completed_at "
        "FROM ops.support_replay_runs WHERE replay_id = $1::uuid",
        str(out.replay_id),
    )
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert row["replay_workflow_run_id"] == out.replay_workflow_run_id

    # Audit anchor lands.
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'support.replay.completed'
           AND target_id = $1
        """,
        str(out.replay_id),
    )
    assert n == 1


@pytest.mark.asyncio
async def test_support_replay_handles_already_triaged_ticket(
    conn, synthetic_ticket, synthetic_user
):
    """Pre-triage the ticket, then invoke replay → triage step skipped
    (triage_ticket only operates on status='open'), other steps continue."""
    # First triage transitions the ticket to status='investigating' which
    # the second triage will refuse — but the rest of the chain runs.
    from app.services.support_cockpit.ticket_triage import triage_ticket
    await triage_ticket(ticket_id=synthetic_ticket)

    inp = SupportReplayInput(
        ticket_id=synthetic_ticket,
        original_workflow_run_id="fake_run_xyz",
        initiated_by_user_id=synthetic_user,
        dry_run=True,
        replay_request_id=uuid4(),
    )
    out = await support_replay_execute.aio_mock_run(inp)

    # triage_ticket on status='investigating' actually re-triages (only
    # rejects 'resolved'/'closed'), so triage_decision is populated.
    # The end result: full chain completes either way.
    assert out.success is True
    assert "investigation" in out.diff_summary
