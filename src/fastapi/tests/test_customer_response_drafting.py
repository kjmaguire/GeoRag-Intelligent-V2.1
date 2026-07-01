"""Live tests for the §25.4 customer_response_drafting agent (doc-phase 143)."""
from __future__ import annotations

import contextlib
import os
from uuid import uuid4

import asyncpg
import pytest

from app.services.support_cockpit.customer_response_drafting import (
    _synthesize_response,
    draft_customer_response,
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
    email = f"test-crd-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        """
        INSERT INTO public.users (name, email, password)
        VALUES ($1, $2, $3) RETURNING id
        """,
        "CRD test user", email, "test-hash",
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
            'wrong_answer', $2, 'high', 'investigating'
        )
        RETURNING ticket_id
        """,
        synthetic_user,
        f"[{prefix}] CRD test ticket — wrong answer",
    )
    try:
        yield tid
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid",
            str(tid),
        )


# ----------------------------------------------------------------------
# Template-engine unit tests
# ----------------------------------------------------------------------
def test_synthesize_response_includes_category_opening():
    r = _synthesize_response(
        category="wrong_answer", severity="high",
        investigation_summary=None,
    )
    assert "wrong answer" in r.lower() or "source-citation" in r


def test_synthesize_response_includes_severity_tone():
    r = _synthesize_response(
        category="other", severity="critical",
        investigation_summary=None,
    )
    assert "critical" in r.lower()
    assert "top-of-queue" in r


def test_synthesize_response_includes_investigation_when_provided():
    r = _synthesize_response(
        category="failed_ingestion", severity="medium",
        investigation_summary="Recent ingest_pdf.failed events (5× in last 7 days)",
    )
    assert "ingest_pdf.failed" in r
    assert "Initial triage notes" in r


def test_synthesize_response_handles_missing_investigation():
    r = _synthesize_response(
        category="performance", severity="low",
        investigation_summary=None,
    )
    assert "still gathering signal" in r
    assert "low" in r.lower()


def test_synthesize_response_carries_doc_phase_marker():
    r = _synthesize_response(
        category="other", severity="medium", investigation_summary=None,
    )
    assert "doc-phase 143" in r
    assert "synthetic_stub" in r


def test_synthesize_response_unknown_category_falls_back_to_other():
    r = _synthesize_response(
        category="not_a_real_category", severity="medium",
        investigation_summary=None,
    )
    # Falls back to the 'other' opening.
    assert "Thanks for reaching out" in r


# ----------------------------------------------------------------------
# End-to-end DB tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_draft_customer_response_end_to_end(
    pool, conn, synthetic_ticket, synthetic_user
):
    """Smoke: draft a response → ticket.customer_visible_response set
    + audit anchor lands."""
    outcome = await draft_customer_response(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        pool=pool,
    )
    assert outcome.category == "wrong_answer"
    assert outcome.severity == "high"
    assert outcome.response_word_count > 30
    assert outcome.drafting_method == "synthetic_stub"

    # Ticket row updated.
    saved = await conn.fetchval(
        "SELECT customer_visible_response FROM ops.support_tickets "
        "WHERE ticket_id = $1::uuid",
        str(synthetic_ticket),
    )
    assert saved is not None
    assert saved == outcome.response_text

    # Audit anchor.
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'support.ticket.response_drafted'
           AND target_id = $1
        """,
        str(synthetic_ticket),
    )
    assert n == 1


@pytest.mark.asyncio
async def test_draft_customer_response_unknown_id_raises(pool, synthetic_user):
    with pytest.raises(ValueError, match="not found"):
        await draft_customer_response(
            ticket_id=uuid4(),
            actor_user_id=synthetic_user,
            pool=pool,
        )


@pytest.mark.asyncio
async def test_draft_customer_response_uses_investigation_summary_when_present(
    pool, conn, synthetic_ticket, synthetic_user
):
    """Insert a synthetic trace summary, draft → response includes it."""
    summary = "Recent fake_signal events (42× in last 7 days)"
    await conn.execute(
        """
        INSERT INTO ops.support_ticket_traces
            (ticket_id, trace_id, trace_summary, added_by_user_id)
        VALUES ($1::uuid, $2, $3, $4)
        """,
        str(synthetic_ticket),
        f"inv_test_{uuid4().hex[:12]}",
        summary,
        synthetic_user,
    )

    outcome = await draft_customer_response(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        pool=pool,
    )
    assert "fake_signal" in outcome.response_text
    assert "42×" in outcome.response_text
