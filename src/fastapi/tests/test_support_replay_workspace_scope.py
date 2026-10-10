"""Support replay for a ticket outside the default tenant, as the AWS worker role
(2026-10 Hatchet audit, finding 22).

WHY THIS EXISTS
    ``ops.support_tickets`` / ``support_replay_runs`` / ``support_ticket_traces``
    carry a STRICT policy after ``db:apply-raw`` (98-rls-tenant-isolation-
    block3.sql): ``workspace_id = NULLIF(current_setting('app.workspace_id',
    true), '')::uuid``, no fail-open branch. The five support agents and the
    support_replay workflow found a ticket's workspace by binding the legacy
    default tenant and reading the ticket, which as ``georag_app`` (NOSUPERUSER
    NOBYPASSRLS, the AWS worker) returns nothing for a ticket in any other
    workspace. The trigger route had already confirmed the ticket exists in the
    caller's workspace, so a replay was accepted and then died in the worker.
    Compose and CI connect as a superuser that bypasses RLS and never showed it.

    The route now sends the authorised ``workspace_id`` in the workflow input
    and every step is scoped straight to it. This module runs that against a
    real non-default workspace as the real role.

HOW TO RUN
    Needs a Postgres where ``php artisan migrate`` AND ``php artisan
    db:apply-raw`` have both run (the ci.yml job "Cron sweeps under
    georag_app"), and a login in ``PG_DSN`` that can ``SET ROLE georag_app``.
    Skips cleanly without one.

        PG_DSN=postgresql://georag:...@localhost:5432/georag \\
            pytest -m integration tests/test_support_replay_workspace_scope.py
"""
from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import asyncpg
import pytest

from app.db import BareConnectionError
from app.hatchet_workflows import support_replay as sr
from app.hatchet_workflows.support_replay import SupportReplayInput
from app.hatchet_workflows.support_replay import execute as support_replay_execute
from app.services.support_cockpit.customer_response_drafting import (
    draft_customer_response,
)
from app.services.support_cockpit.escalation_routing import route_escalation
from app.services.support_cockpit.root_cause_investigation import investigate_ticket
from app.services.support_cockpit.support_packet import build_support_packet
from app.services.support_cockpit.ticket_triage import triage_ticket

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get(
    "PG_DSN", "postgresql://georag:georag_dev_password@localhost:5432/georag",
)
APP_ROLE = os.environ.get("PG_APP_ROLE", "georag_app")
DEFAULT_WORKSPACE = "a0000000-0000-0000-0000-000000000001"


async def _as_app_role(conn: asyncpg.Connection) -> None:
    await conn.execute(f"SET ROLE {APP_ROLE}")


@pytest.fixture
async def owner_conn() -> AsyncIterator[asyncpg.Connection]:
    try:
        conn = await asyncpg.connect(PG_DSN, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no Postgres at PG_DSN ({exc})")
    try:
        forced = await conn.fetchval(
            "SELECT c.relforcerowsecurity FROM pg_class c "
            " WHERE c.oid = to_regclass('ops.support_tickets')",
        )
        if not forced:
            pytest.skip("ops.support_tickets has no forced RLS; run migrate + db:apply-raw first")
        if not await conn.fetchval(
            "SELECT pg_has_role(current_user, $1, 'MEMBER')", APP_ROLE,
        ):
            pytest.skip(f"the PG_DSN login cannot SET ROLE {APP_ROLE}")
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def app_pool(owner_conn, monkeypatch) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(
        PG_DSN, min_size=1, max_size=4, statement_cache_size=0, init=_as_app_role,
    )
    async with pool.acquire() as conn:
        flags = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
        )
    # The whole point: if the pool bypasses RLS every assertion below passes for
    # the wrong reason.
    assert flags is not None and not flags["rolsuper"] and not flags["rolbypassrls"]

    # The workflow body opens its own pool; make it the app role too.
    real_create_pool = asyncpg.create_pool

    def _app_role_pool(*args, **kwargs):
        kwargs["init"] = _as_app_role
        return real_create_pool(*args, **kwargs)

    monkeypatch.setattr(sr, "_dsn", lambda: PG_DSN)
    monkeypatch.setattr(asyncpg, "create_pool", _app_role_pool)
    try:
        yield pool
    finally:
        await pool.close()


class _Seed(SimpleNamespace):
    """A user, a non-default workspace with a ticket, and a default-tenant ticket."""


@pytest.fixture
async def seed(owner_conn) -> AsyncIterator[_Seed]:
    tag = uuid.uuid4().hex[:8]
    ws = str(uuid.uuid4())
    user_id = await owner_conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1, $2, 'x') RETURNING id",
        f"scope {tag}", f"scope-{tag}@example.test",
    )
    await owner_conn.execute(
        "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1::uuid, $2, $2)",
        ws, f"scope-{tag}",
    )
    insert = (
        "INSERT INTO ops.support_tickets (workspace_id, reported_by_user_id, channel, "
        "category, description, severity, status) "
        "VALUES ($1::uuid, $2, 'in_app', 'failed_ingestion', $3, 'high', 'investigating') "
        "RETURNING ticket_id::text"
    )
    s = _Seed(
        ws=ws,
        user=user_id,
        ticket=await owner_conn.fetchval(insert, ws, user_id, f"[{tag}] PDF upload crashed"),
        default_ticket=await owner_conn.fetchval(
            insert, DEFAULT_WORKSPACE, user_id, f"[{tag}] default-tenant ticket",
        ),
    )
    try:
        yield s
    finally:
        await owner_conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = ANY($1::uuid[])",
            [s.ticket, s.default_ticket],
        )
        async with owner_conn.transaction():
            # The ledger is append-only; these are the test database's own rows.
            await owner_conn.execute("SET LOCAL session_replication_role = replica")
            await owner_conn.execute(
                "DELETE FROM audit.audit_ledger WHERE workspace_id = $1::uuid "
                "OR target_id = ANY($2::text[])",
                ws, [s.ticket, s.default_ticket],
            )
        await owner_conn.execute("DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", ws)
        with contextlib.suppress(asyncpg.ForeignKeyViolationError):
            await owner_conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


def _request(seed: _Seed, *, ticket: str | None = None, workspace: str | None = None,
             dry_run: bool = True, request_id: uuid.UUID | None = None) -> SupportReplayInput:
    return SupportReplayInput(
        ticket_id=ticket or seed.ticket,
        original_workflow_run_id="scope-test-run",
        initiated_by_user_id=seed.user,
        dry_run=dry_run,
        replay_request_id=request_id or uuid.uuid4(),
        workspace_id=workspace,
    )


async def _ticket_changed(owner_conn: asyncpg.Connection, ticket: str) -> tuple:
    row = await owner_conn.fetchrow(
        "SELECT severity, status, customer_visible_response, "
        "       (SELECT count(*) FROM ops.support_ticket_traces t WHERE t.ticket_id = x.ticket_id), "
        "       (SELECT count(*) FROM audit.audit_ledger a WHERE a.target_id = x.ticket_id::text) "
        "  FROM ops.support_tickets x WHERE ticket_id = $1::uuid",
        ticket,
    )
    return tuple(row)


# ---------------------------------------------------------------------------
# The premise
# ---------------------------------------------------------------------------
async def test_the_app_role_cannot_discover_another_workspaces_ticket(app_pool, seed) -> None:
    """The failure the fix is built on. Reading the ticket under the default
    tenant to learn its workspace finds nothing for any other workspace. If this
    starts finding the ticket, ops.support_* has gone fail-open, which is its own
    incident."""
    with pytest.raises(ValueError, match="not found"):
        await triage_ticket(ticket_id=seed.ticket, pool=app_pool)

    # ...but a ticket in the default tenant is still found that way.
    outcome = await triage_ticket(ticket_id=seed.default_ticket, pool=app_pool, dry_run=True)
    assert outcome.new_status == "investigating"


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------
async def test_every_agent_runs_for_a_non_default_workspace_given_the_workspace(
    app_pool, owner_conn, seed
) -> None:
    ws = seed.ws
    kw = {"pool": app_pool, "workspace_id": ws, "dry_run": False}

    triage = await triage_ticket(ticket_id=seed.ticket, **kw)
    assert triage.new_severity == "critical"
    inv = await investigate_ticket(ticket_id=seed.ticket, actor_user_id=seed.user, **kw)
    packet = await build_support_packet(ticket_id=seed.ticket, **kw)
    assert packet.packet_anchor_id is not None
    draft = await draft_customer_response(ticket_id=seed.ticket, actor_user_id=seed.user, **kw)
    routed = await route_escalation(ticket_id=seed.ticket, actor_user_id=seed.user, **kw)
    assert routed.has_investigation and routed.has_response_draft

    # What landed, landed in THIS workspace (the trace row's workspace_id is NOT
    # NULL under the raw layer and the INSERT used to leave it out).
    trace_ws = await owner_conn.fetchval(
        "SELECT workspace_id::text FROM ops.support_ticket_traces WHERE trace_id = $1", inv.trace_id,
    )
    assert trace_ws == ws
    stored = await owner_conn.fetchval(
        "SELECT customer_visible_response FROM ops.support_tickets WHERE ticket_id = $1::uuid",
        seed.ticket,
    )
    assert stored == draft.response_text
    anchors = await owner_conn.fetch(
        "SELECT DISTINCT workspace_id::text AS ws FROM audit.audit_ledger WHERE target_id = $1",
        seed.ticket,
    )
    assert [a["ws"] for a in anchors] == [ws]


async def test_a_dry_run_for_a_non_default_workspace_writes_nothing(
    app_pool, owner_conn, seed
) -> None:
    before = await _ticket_changed(owner_conn, seed.ticket)
    kw = {"pool": app_pool, "workspace_id": seed.ws, "dry_run": True}

    await triage_ticket(ticket_id=seed.ticket, **kw)
    await investigate_ticket(ticket_id=seed.ticket, actor_user_id=seed.user, **kw)
    await build_support_packet(ticket_id=seed.ticket, **kw)
    await draft_customer_response(ticket_id=seed.ticket, actor_user_id=seed.user, **kw)
    await route_escalation(
        ticket_id=seed.ticket, actor_user_id=seed.user, assign_to_user_id=seed.user, **kw,
    )

    assert await _ticket_changed(owner_conn, seed.ticket) == before


async def test_the_workflow_replays_a_non_default_workspace_ticket(
    app_pool, owner_conn, seed
) -> None:
    """The whole route -> worker path as georag_app: what the trigger route now
    dispatches (workspace_id in the input) completes, writes its bookkeeping row
    in the ticket's workspace and leaves the ticket alone."""
    before = await _ticket_changed(owner_conn, seed.ticket)
    inp = _request(seed, workspace=seed.ws)

    out = await support_replay_execute.aio_mock_run(inp)

    assert out.success is True and out.duplicate is False
    row = await owner_conn.fetchrow(
        "SELECT status, dry_run, workspace_id::text AS ws FROM ops.support_replay_runs "
        " WHERE replay_request_id = $1::uuid",
        str(inp.replay_request_id),
    )
    assert (row["status"], row["dry_run"], row["ws"]) == ("completed", True, seed.ws)
    n = await owner_conn.fetchval(
        "SELECT count(*) FROM audit.audit_ledger "
        " WHERE action_type = 'support.replay.completed' AND target_id = $1",
        str(out.replay_id),
    )
    assert n == 1
    # The replay's own anchor targets the replay, not the ticket.
    assert await _ticket_changed(owner_conn, seed.ticket) == before


async def test_a_replay_started_by_hand_without_a_workspace_fails_loud_before_writing(
    app_pool, owner_conn, seed
) -> None:
    """Started from the Hatchet UI there is no trigger route to supply the
    workspace, so the old discovery runs -- and for a non-default ticket under
    this role it finds nothing. That must be an error with nothing written, not a
    half-run."""
    inp = _request(seed)  # no workspace_id
    with pytest.raises(BareConnectionError):
        await support_replay_execute.aio_mock_run(inp)
    assert await owner_conn.fetchval(
        "SELECT count(*) FROM ops.support_replay_runs WHERE replay_request_id = $1::uuid",
        str(inp.replay_request_id),
    ) == 0


async def test_a_workspace_that_does_not_own_the_ticket_is_refused(
    app_pool, owner_conn, seed
) -> None:
    """Supplying a workspace_id is not a way across tenants: RLS still decides
    what the scoped connection can read."""
    inp = _request(seed, workspace=DEFAULT_WORKSPACE)  # the ticket is in seed.ws
    with pytest.raises(BareConnectionError, match="no rows"):
        await support_replay_execute.aio_mock_run(inp)
    assert await owner_conn.fetchval(
        "SELECT count(*) FROM ops.support_replay_runs WHERE replay_request_id = $1::uuid",
        str(inp.replay_request_id),
    ) == 0


async def test_a_request_id_used_in_another_workspace_is_refused(
    app_pool, owner_conn, seed
) -> None:
    """The unique index spans workspaces but RLS hides the other tenant's row.
    The second claim must fail loud rather than answer with, or dereference, a
    row it cannot see."""
    request_id = uuid.uuid4()
    await support_replay_execute.aio_mock_run(_request(seed, workspace=seed.ws, request_id=request_id))

    clash = _request(
        seed, ticket=seed.default_ticket, workspace=DEFAULT_WORKSPACE, request_id=request_id,
    )
    with pytest.raises(RuntimeError, match="different ticket"):
        await support_replay_execute.aio_mock_run(clash)


async def test_the_failure_hook_closes_a_non_default_workspace_row(app_pool, owner_conn, seed) -> None:
    inp = _request(seed, workspace=seed.ws)
    claim = await sr._claim_replay(app_pool, inp)  # the body died right after this

    closed = await sr.close_replay_after_workflow_failure(
        inp, SimpleNamespace(task_run_errors={"execute": "worker lost"}),
    )

    assert closed == {"updated": True, "replay_id": str(claim.replay_id)}
    status = await owner_conn.fetchval(
        "SELECT status FROM ops.support_replay_runs WHERE replay_id = $1::uuid",
        str(claim.replay_id),
    )
    assert status == "failed"
