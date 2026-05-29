"""Live tests for the §25.4 escalation_routing agent (doc-phase 144)."""
from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from app.services.support_cockpit.escalation_routing import (
    _synthetic_router,
    route_escalation,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        yield p
    finally:
        await p.close()


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
    email = f"test-er-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        """
        INSERT INTO public.users (name, email, password)
        VALUES ($1, $2, $3) RETURNING id
        """,
        "ER test user", email, "test-hash",
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
            'integration_issue', $2, 'high', 'investigating'
        )
        RETURNING ticket_id
        """,
        synthetic_user,
        f"[{prefix}] ER test ticket",
    )
    try:
        yield tid
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid",
            str(tid),
        )


# ----------------------------------------------------------------------
# Router decision-tree unit tests
# ----------------------------------------------------------------------
def test_router_critical_always_pages_on_call():
    d, _ = _synthetic_router(
        severity="critical", category="failed_ingestion",
        has_triage=True, has_investigation=True, has_response_draft=True,
    )
    assert d == "on_call_engineer"


def test_router_critical_overrides_other_signals():
    """Even with no other signal, critical pages on-call."""
    d, _ = _synthetic_router(
        severity="critical", category="other",
        has_triage=False, has_investigation=False, has_response_draft=False,
    )
    assert d == "on_call_engineer"


def test_router_wrong_answer_with_investigation_goes_to_sme():
    d, _ = _synthetic_router(
        severity="high", category="wrong_answer",
        has_triage=True, has_investigation=True, has_response_draft=False,
    )
    assert d == "sme_review"


def test_router_wrong_answer_without_investigation_waits_for_signal():
    d, _ = _synthetic_router(
        severity="medium", category="wrong_answer",
        has_triage=True, has_investigation=False, has_response_draft=False,
    )
    assert d == "wait_for_more_signal"


def test_router_failed_report_with_draft_goes_to_sme():
    d, _ = _synthetic_router(
        severity="high", category="failed_report",
        has_triage=True, has_investigation=True, has_response_draft=True,
    )
    assert d == "sme_review"


def test_router_high_severity_default_to_engineer_queue():
    d, _ = _synthetic_router(
        severity="high", category="performance",
        has_triage=True, has_investigation=False, has_response_draft=False,
    )
    assert d == "queue_for_engineer"


def test_router_low_severity_full_chain_auto_resolves():
    d, _ = _synthetic_router(
        severity="low", category="other",
        has_triage=True, has_investigation=True, has_response_draft=True,
    )
    assert d == "auto_resolve"


def test_router_medium_default_to_engineer_queue():
    d, _ = _synthetic_router(
        severity="medium", category="performance",
        has_triage=True, has_investigation=True, has_response_draft=True,
    )
    assert d == "queue_for_engineer"


# ----------------------------------------------------------------------
# End-to-end DB tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_route_escalation_end_to_end(
    pool, conn, synthetic_ticket, synthetic_user
):
    """Smoke: route a ticket → audit anchor lands with decision + rationale."""
    outcome = await route_escalation(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        pool=pool,
    )
    # high severity → queue_for_engineer (per the decision tree).
    assert outcome.decision == "queue_for_engineer"
    assert outcome.severity == "high"
    assert outcome.category == "integration_issue"
    assert outcome.routing_method == "synthetic_stub"
    assert outcome.rationale

    # Audit anchor.
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'support.ticket.escalation_routed'
           AND target_id = $1
        """,
        str(synthetic_ticket),
    )
    assert n == 1


@pytest.mark.asyncio
async def test_route_escalation_assigns_user_when_provided(
    pool, conn, synthetic_ticket, synthetic_user
):
    """When assign_to_user_id is supplied, ticket gets assigned."""
    outcome = await route_escalation(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        assign_to_user_id=synthetic_user,
        pool=pool,
    )
    assert outcome.assigned_to_user_id == synthetic_user

    saved = await conn.fetchval(
        "SELECT assigned_to_user_id FROM ops.support_tickets "
        "WHERE ticket_id = $1::uuid",
        str(synthetic_ticket),
    )
    assert saved == synthetic_user


@pytest.mark.asyncio
async def test_route_escalation_preserves_existing_assignment(
    pool, conn, synthetic_ticket, synthetic_user
):
    """When assign_to_user_id is None, existing assignment is preserved."""
    # Pre-assign.
    await conn.execute(
        "UPDATE ops.support_tickets SET assigned_to_user_id = $1 "
        "WHERE ticket_id = $2::uuid",
        synthetic_user, str(synthetic_ticket),
    )

    outcome = await route_escalation(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        pool=pool,  # no assign_to_user_id
    )
    assert outcome.assigned_to_user_id == synthetic_user


@pytest.mark.asyncio
async def test_route_escalation_unknown_id_raises(pool, synthetic_user):
    with pytest.raises(ValueError, match="not found"):
        await route_escalation(
            ticket_id=uuid4(),
            actor_user_id=synthetic_user,
            pool=pool,
        )
