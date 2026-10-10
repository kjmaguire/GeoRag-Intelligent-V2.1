"""Live test for the doc-phase 146 support_replay Hatchet workflow body.

Inserts a synthetic ticket, invokes the workflow body via
.aio_mock_run, asserts the replay row + chain results + audit anchor.

2026-10-10 (Hatchet audit, finding 7): a dry run writes nothing to the ticket, a
repeated replay_request_id runs the chain once, and a replay row is never left
'running'. The same-workspace-under-a-NOBYPASSRLS-role half (finding 22) is in
test_support_replay_workspace_scope.py, which needs the raw RLS layer.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from app.hatchet_workflows import support_replay as sr
from app.hatchet_workflows.support_replay import (
    SupportReplayInput,
)
from app.hatchet_workflows.support_replay import (
    execute as support_replay_execute,
)

# CI's non-integration pytest job runs without a database service; these tests
# dial Postgres at fixture setup, so they must run in the integration lane.
pytestmark = pytest.mark.integration

DEFAULT_WORKSPACE = "a0000000-0000-0000-0000-000000000001"

CHAIN_ANCHORS = {
    "support.ticket.triaged",
    "support.ticket.investigated",
    "support.packet.assembled",
    "support.ticket.response_drafted",
    "support.ticket.escalation_routed",
}


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
        DEFAULT_WORKSPACE,
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
        with contextlib.suppress(asyncpg.ForeignKeyViolationError):
            await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


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


def _request(ticket_id, user_id, *, dry_run: bool = True, workspace: str | None = None):
    return SupportReplayInput(
        ticket_id=ticket_id,
        original_workflow_run_id="fake_original_run_id_abc123",
        initiated_by_user_id=user_id,
        dry_run=dry_run,
        replay_request_id=uuid4(),
        workspace_id=workspace,
    )


async def _ticket_state(conn: asyncpg.Connection, ticket_id) -> dict:
    """Everything the support chain can change on a ticket, in one dict."""
    row = await conn.fetchrow(
        "SELECT severity, category, status, assigned_to_user_id, "
        "       customer_visible_response "
        "  FROM ops.support_tickets WHERE ticket_id = $1::uuid",
        str(ticket_id),
    )
    traces = await conn.fetchval(
        "SELECT count(*) FROM ops.support_ticket_traces WHERE ticket_id = $1::uuid",
        str(ticket_id),
    )
    anchors = await conn.fetch(
        "SELECT action_type FROM audit.audit_ledger WHERE target_id = $1 "
        "ORDER BY created_at, id",
        str(ticket_id),
    )
    return {**dict(row), "traces": traces, "anchors": [a["action_type"] for a in anchors]}


async def _replay_row(conn: asyncpg.Connection, request_id) -> asyncpg.Record:
    return await conn.fetchrow(
        "SELECT replay_id, status, dry_run, diff_summary, replay_workflow_run_id, "
        "       completed_at, workspace_id::text AS workspace_id, replay_request_id "
        "  FROM ops.support_replay_runs WHERE replay_request_id = $1::uuid",
        str(request_id),
    )


async def _replay_anchors(conn: asyncpg.Connection, replay_id) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM audit.audit_ledger "
        " WHERE action_type = 'support.replay.completed' AND target_id = $1",
        str(replay_id),
    )


@pytest.mark.asyncio
async def test_support_replay_runs_full_chain(conn, synthetic_ticket, synthetic_user):
    """End-to-end: invoke the workflow body, assert replay row +
    chain results + audit anchor."""
    inp = _request(synthetic_ticket, synthetic_user)
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
    # A dry run links no trace, so there is no trace id to hand back.
    assert out.investigation_trace_id is None
    assert out.replay_workflow_run_id is not None
    assert out.replay_workflow_run_id.startswith("replay_")

    # Replay row persists with status='completed'.
    row = await _replay_row(conn, inp.replay_request_id)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert row["replay_workflow_run_id"] == out.replay_workflow_run_id
    assert row["dry_run"] is True
    assert row["workspace_id"] == DEFAULT_WORKSPACE

    # Audit anchor lands.
    assert await _replay_anchors(conn, out.replay_id) == 1


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

    inp = _request(synthetic_ticket, synthetic_user)
    out = await support_replay_execute.aio_mock_run(inp)

    # triage_ticket on status='investigating' actually re-triages (only
    # rejects 'resolved'/'closed'), so triage_decision is populated.
    # The end result: full chain completes either way.
    assert out.success is True
    assert "investigation" in out.diff_summary


# ---------------------------------------------------------------------------
# Finding 7 -- a dry run writes nothing to the ticket
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [None, DEFAULT_WORKSPACE], ids=["discovered", "supplied"])
async def test_a_dry_run_replay_writes_nothing_to_the_ticket(
    conn, synthetic_ticket, synthetic_user, workspace
):
    """The route only ever dispatches dry_run=true, and a dry run used to
    re-triage the ticket (here high -> critical), overwrite its customer-visible
    draft, link a trace and emit five anchors. It now leaves the ticket, its
    trace links and its audit chain exactly as they were; the only thing it
    writes is its own bookkeeping."""
    before = await _ticket_state(conn, synthetic_ticket)
    assert before["severity"] == "high"
    assert before["customer_visible_response"] is None

    inp = _request(synthetic_ticket, synthetic_user, dry_run=True, workspace=workspace)
    out = await support_replay_execute.aio_mock_run(inp)

    # First, and on its own: this is the assertion the old code failed (the
    # ticket came back critical, with a draft, a trace link and five anchors).
    assert await _ticket_state(conn, synthetic_ticket) == before

    assert out.success is True
    assert "dry run" in out.diff_summary
    assert "not anchored" in out.diff_summary

    # Its own record of having looked, and nothing else.
    row = await _replay_row(conn, inp.replay_request_id)
    assert (row["status"], row["dry_run"]) == ("completed", True)
    assert await _replay_anchors(conn, out.replay_id) == 1


@pytest.mark.asyncio
async def test_a_live_replay_still_writes_what_a_dry_run_must_not(
    conn, synthetic_ticket, synthetic_user
):
    """The flag is what separates them: the workflow body is unchanged for a
    live run (the trigger route never dispatches one)."""
    inp = _request(synthetic_ticket, synthetic_user, dry_run=False)
    out = await support_replay_execute.aio_mock_run(inp)

    after = await _ticket_state(conn, synthetic_ticket)
    assert after["severity"] == "critical"  # re-triaged: 'crashed'
    assert after["customer_visible_response"] is not None
    assert after["traces"] == 1
    assert set(after["anchors"]) >= CHAIN_ANCHORS
    assert out.investigation_trace_id is not None
    assert "dry run" not in out.diff_summary


@pytest.mark.asyncio
async def test_each_agent_honours_dry_run_on_its_own(conn, synthetic_ticket, synthetic_user):
    """Every service of the chain, called directly, returns its result and
    writes nothing -- including the assignment route_escalation would make."""
    from app.services.support_cockpit.customer_response_drafting import (
        draft_customer_response,
    )
    from app.services.support_cockpit.escalation_routing import route_escalation
    from app.services.support_cockpit.root_cause_investigation import (
        investigate_ticket,
    )
    from app.services.support_cockpit.support_packet import build_support_packet
    from app.services.support_cockpit.ticket_triage import triage_ticket

    before = await _ticket_state(conn, synthetic_ticket)
    ws = DEFAULT_WORKSPACE

    triage = await triage_ticket(ticket_id=synthetic_ticket, workspace_id=ws, dry_run=True)
    assert (triage.prior_severity, triage.new_severity) == ("high", "critical")

    inv = await investigate_ticket(
        ticket_id=synthetic_ticket, actor_user_id=synthetic_user,
        workspace_id=ws, dry_run=True,
    )
    assert inv.trace_id.startswith("inv_")

    packet = await build_support_packet(ticket_id=synthetic_ticket, workspace_id=ws, dry_run=True)
    assert packet.packet_anchor_id is None

    draft = await draft_customer_response(
        ticket_id=synthetic_ticket, actor_user_id=synthetic_user,
        workspace_id=ws, dry_run=True,
    )
    assert draft.response_word_count > 30

    routed = await route_escalation(
        ticket_id=synthetic_ticket, actor_user_id=synthetic_user,
        assign_to_user_id=synthetic_user, workspace_id=ws, dry_run=True,
    )
    assert routed.assigned_to_user_id == synthetic_user  # what it WOULD assign

    assert await _ticket_state(conn, synthetic_ticket) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [None, DEFAULT_WORKSPACE], ids=["discovered", "supplied"])
async def test_the_dry_run_transaction_is_read_only_at_the_database(
    conn, synthetic_ticket, workspace
):
    """The backstop under the per-agent `if not dry_run`: a code path that
    forgets the flag fails instead of mutating a live ticket."""
    from app.services.support_cockpit._scope import ticket_connection

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=1, statement_cache_size=0)
    try:
        async with ticket_connection(
            pool,
            ticket_id=str(synthetic_ticket),
            lookup_sql="SELECT workspace_id::text AS workspace_id FROM ops.support_tickets "
                       "WHERE ticket_id = $1::uuid",
            site="test.read_only",
            bootstrap_reason="support_cockpit.elevated_lookup",
            workspace_id=workspace,
            read_only=True,
        ) as (c, _row):
            with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
                await c.execute(
                    "UPDATE ops.support_tickets SET severity = 'low' WHERE ticket_id = $1::uuid",
                    str(synthetic_ticket),
                )
    finally:
        await pool.close()
    assert (await _ticket_state(conn, synthetic_ticket))["severity"] == "high"


# ---------------------------------------------------------------------------
# Finding 7 -- idempotent on replay_request_id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "live"])
async def test_a_repeated_dispatch_runs_the_chain_once(
    conn, synthetic_ticket, synthetic_user, dry_run
):
    """Laravel retries a dispatch that timed out on the read; the second one
    must find the first run's row, not run the chain (and write every anchor)
    again."""
    inp = _request(synthetic_ticket, synthetic_user, dry_run=dry_run)
    first = await support_replay_execute.aio_mock_run(inp)
    second = await support_replay_execute.aio_mock_run(inp)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.replay_id == first.replay_id
    assert second.success is True
    assert second.diff_summary == first.diff_summary
    assert await conn.fetchval(
        "SELECT count(*) FROM ops.support_replay_runs WHERE replay_request_id = $1::uuid",
        str(inp.replay_request_id),
    ) == 1
    assert await _replay_anchors(conn, first.replay_id) == 1
    anchors = (await _ticket_state(conn, synthetic_ticket))["anchors"]
    assert anchors.count("support.ticket.triaged") == (0 if dry_run else 1)


@pytest.mark.asyncio
async def test_two_dispatches_at_once_run_the_chain_once(conn, synthetic_ticket, synthetic_user):
    """The unique index decides, not a read-then-write."""
    inp = _request(synthetic_ticket, synthetic_user, dry_run=False)
    results = await asyncio.gather(
        support_replay_execute.aio_mock_run(inp),
        support_replay_execute.aio_mock_run(inp),
    )

    assert sorted(r.duplicate for r in results) == [False, True]
    assert results[0].replay_id == results[1].replay_id
    anchors = (await _ticket_state(conn, synthetic_ticket))["anchors"]
    assert anchors.count("support.ticket.triaged") == 1


@pytest.mark.asyncio
async def test_a_request_id_that_belongs_to_another_ticket_is_refused(
    conn, synthetic_ticket, synthetic_user
):
    """The key identifies ONE request. Reusing it for a different ticket is a
    caller bug and must not be answered with the other ticket's result."""
    other = await conn.fetchval(
        "INSERT INTO ops.support_tickets (workspace_id, reported_by_user_id, channel, "
        "category, description, severity, status) VALUES ($1::uuid, $2, 'in_app', 'other', "
        "'another ticket', 'low', 'open') RETURNING ticket_id",
        DEFAULT_WORKSPACE, synthetic_user,
    )
    try:
        first = _request(synthetic_ticket, synthetic_user)
        await support_replay_execute.aio_mock_run(first)
        clash = first.model_copy(update={"ticket_id": other})
        with pytest.raises(RuntimeError, match="different ticket"):
            await support_replay_execute.aio_mock_run(clash)
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid", str(other),
        )


# ---------------------------------------------------------------------------
# Finding 7 -- a row is never left 'running'
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_failed_chain_fails_the_run_and_closes_the_row(
    monkeypatch, conn, synthetic_ticket, synthetic_user
):
    async def _boom(**_kw):
        raise RuntimeError("investigation exploded")

    monkeypatch.setattr(sr, "investigate_ticket", _boom)
    inp = _request(synthetic_ticket, synthetic_user)

    # The run fails (it used to return success=False and show green)...
    with pytest.raises(RuntimeError, match="investigation exploded"):
        await support_replay_execute.aio_mock_run(inp)

    # ...and the row says why.
    row = await _replay_row(conn, inp.replay_request_id)
    assert row["status"] == "failed"
    assert row["completed_at"] is not None
    assert "failed: investigation exploded" in row["diff_summary"]
    assert "triage" in row["diff_summary"]  # the steps that did run
    assert await _replay_anchors(conn, row["replay_id"]) == 1


@pytest.mark.asyncio
async def test_a_cancelled_replay_does_not_stay_running(
    monkeypatch, conn, synthetic_ticket, synthetic_user
):
    """CancelledError is a BaseException: the chain's `except Exception` does
    not see it, and the row used to stay 'running' for good."""
    async def _cancelled(**_kw):
        raise asyncio.CancelledError

    monkeypatch.setattr(sr, "investigate_ticket", _cancelled)
    inp = _request(synthetic_ticket, synthetic_user)

    with pytest.raises(asyncio.CancelledError):
        await support_replay_execute.aio_mock_run(inp)

    row = await _replay_row(conn, inp.replay_request_id)
    assert row["status"] == "failed"
    assert row["completed_at"] is not None
    assert "CancelledError" in row["diff_summary"]


@pytest.mark.asyncio
async def test_a_failed_completion_write_does_not_leave_the_row_running(
    monkeypatch, conn, synthetic_ticket, synthetic_user
):
    """The completion UPDATE and the anchor share a transaction; if the anchor
    fails the UPDATE rolls back and the row would read 'running'."""
    async def _ledger_down(*_a, **_kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(sr, "emit_audit", _ledger_down)
    inp = _request(synthetic_ticket, synthetic_user)

    with pytest.raises(RuntimeError, match="ledger down"):
        await support_replay_execute.aio_mock_run(inp)

    row = await _replay_row(conn, inp.replay_request_id)
    assert row["status"] == "failed"
    assert "ledger down" in row["diff_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [None, DEFAULT_WORKSPACE], ids=["discovered", "supplied"])
async def test_the_failure_hook_closes_a_row_the_body_left_running(
    conn, synthetic_ticket, synthetic_user, workspace
):
    """Timeout, worker loss, a cancel after the claim: the body never gets to
    close its row, so the on_failure hook does, found by replay_request_id."""
    inp = _request(synthetic_ticket, synthetic_user, workspace=workspace)
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=1, statement_cache_size=0)
    try:
        claim = await sr._claim_replay(pool, inp)  # the body died right after this
    finally:
        await pool.close()
    assert (await _replay_row(conn, inp.replay_request_id))["status"] == "running"

    ctx = SimpleNamespace(task_run_errors={"execute": "execution timed out after 1h"})
    closed = await sr.close_replay_after_workflow_failure(inp, ctx)

    assert closed == {"updated": True, "replay_id": str(claim.replay_id)}
    row = await _replay_row(conn, inp.replay_request_id)
    assert row["status"] == "failed"
    assert row["completed_at"] is not None
    assert "execute: execution timed out after 1h" in row["diff_summary"]

    # Idempotent: the hook retries (retries=2) and may race the body.
    again = await sr.close_replay_after_workflow_failure(inp, ctx)
    assert again == {"updated": False, "reason": "no_running_row"}


@pytest.mark.asyncio
async def test_the_failure_hook_leaves_a_finished_row_alone(
    conn, synthetic_ticket, synthetic_user
):
    inp = _request(synthetic_ticket, synthetic_user)
    await support_replay_execute.aio_mock_run(inp)

    result = await sr.close_replay_after_workflow_failure(inp, SimpleNamespace())

    assert result == {"updated": False, "reason": "no_running_row"}
    assert (await _replay_row(conn, inp.replay_request_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_the_failure_hook_for_a_run_that_never_claimed_a_row(synthetic_user):
    """A cancel that lands before the body starts: no row, nothing to close,
    and no exception to fail the hook."""
    inp = _request(uuid4(), synthetic_user)
    result = await sr.close_replay_after_workflow_failure(inp, SimpleNamespace())
    assert result == {"updated": False, "reason": "no_ticket"}


@pytest.mark.asyncio
async def test_the_replay_request_id_index_is_unique(conn) -> None:
    """The dedupe is only as good as the index: ON CONFLICT (replay_request_id)
    needs a UNIQUE index on exactly that column."""
    unique, cols = await conn.fetchrow(
        "SELECT i.indisunique, "
        "       array_agg(a.attname ORDER BY k.ord) "
        "  FROM pg_index i "
        "  JOIN pg_class c ON c.oid = i.indexrelid "
        "  JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) ON true "
        "  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
        " WHERE c.relname = 'support_replay_runs_request_id_uq' "
        " GROUP BY i.indisunique",
    )
    assert unique is True
    assert cols == ["replay_request_id"]
