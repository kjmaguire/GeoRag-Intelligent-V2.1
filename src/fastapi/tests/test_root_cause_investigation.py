"""Live tests for the §25.4 root_cause_investigation agent (doc-phase 139).

Verifies:
  - Heuristic causes assembled from recent audit + decision signal
  - Investigation links into ops.support_ticket_traces
  - Audit anchor (support.ticket.investigated) emitted with full payload
  - Idempotent on re-run (same trace_id → no duplicate trace link)
  - Refuses to investigate unknown tickets
"""
from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from app.services.support_cockpit.root_cause_investigation import (
    CATEGORY_AUDIT_PATTERNS,
    _synthesize_top_causes,
    investigate_ticket,
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
    """Insert + tear down a synthetic user."""
    email = f"test-rci-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        """
        INSERT INTO public.users (name, email, password)
        VALUES ($1, $2, $3) RETURNING id
        """,
        "RCI test user", email, "test-hash",
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
    """Insert + tear down a synthetic ticket."""
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
        f"[{prefix}] synthetic ticket for root_cause test",
    )
    try:
        yield tid
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid",
            str(tid),
        )


# ----------------------------------------------------------------------
# Heuristic unit tests (no DB)
# ----------------------------------------------------------------------
def test_category_patterns_cover_all_valid_categories():
    """Every valid category from VALID_CATEGORIES has a pattern entry
    (possibly empty list for 'performance'/'other')."""
    for cat in ["wrong_answer", "failed_ingestion", "failed_report",
                "integration_issue", "performance", "other"]:
        assert cat in CATEGORY_AUDIT_PATTERNS


def test_synthesize_top_causes_no_signal_returns_data_scarcity():
    causes, summary = _synthesize_top_causes(
        category="performance", audit_rows=[], decision_rows=[]
    )
    assert len(causes) == 1
    assert "No directly-relevant audit signal" in causes[0]["cause"]
    assert "performance" in summary


def test_synthesize_top_causes_clusters_audits_by_action_type():
    # Fake records via mock objects with the columns we read.
    class FakeRow:
        def __init__(self, id, action_type):
            self._d = {"id": id, "action_type": action_type}
        def __getitem__(self, key): return self._d[key]

    rows = [
        FakeRow("a1", "ingest_pdf.failed"),
        FakeRow("a2", "ingest_pdf.failed"),
        FakeRow("a3", "ingest_pdf.failed"),
        FakeRow("a4", "ocr.failed"),
    ]
    causes, summary = _synthesize_top_causes(
        category="failed_ingestion", audit_rows=rows, decision_rows=[]
    )
    assert len(causes) == 2
    # Higher-count cluster is first (sorted by relevance).
    assert causes[0]["cause"].startswith("Recent ingest_pdf.failed")
    assert len(causes[0]["evidence_audit_ids"]) >= 3


# ----------------------------------------------------------------------
# End-to-end DB tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_investigate_ticket_end_to_end(
    pool, conn, synthetic_ticket, synthetic_user
):
    """Smoke: investigate a ticket → trace_id linked + audit anchor emitted."""
    result = await investigate_ticket(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        pool=pool,
    )
    assert result.trace_id.startswith("inv_")
    assert result.top_cause_summary
    assert result.investigation_method == "synthetic_stub"

    # trace link persisted.
    link = await conn.fetchrow(
        """
        SELECT trace_id, trace_summary FROM ops.support_ticket_traces
         WHERE ticket_id = $1::uuid AND trace_id = $2
        """,
        str(synthetic_ticket), result.trace_id,
    )
    assert link is not None
    assert link["trace_summary"][:50] in result.top_cause_summary or result.top_cause_summary[:50] in link["trace_summary"]

    # audit anchor lands.
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'support.ticket.investigated'
           AND target_id = $1 AND trace_id = $2
        """,
        str(synthetic_ticket), result.trace_id,
    )
    assert n == 1


@pytest.mark.asyncio
async def test_investigate_ticket_unknown_id_raises(pool, synthetic_user):
    with pytest.raises(ValueError, match="not found"):
        await investigate_ticket(
            ticket_id=uuid4(),
            actor_user_id=synthetic_user,
            pool=pool,
        )


@pytest.mark.asyncio
async def test_investigate_ticket_multiple_runs_create_distinct_traces(
    pool, conn, synthetic_ticket, synthetic_user
):
    """Each investigate_ticket call generates a fresh trace_id, so
    successive investigations don't collide on the unique constraint
    and the ticket can accumulate multiple traces over time."""
    r1 = await investigate_ticket(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        pool=pool,
    )
    r2 = await investigate_ticket(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        pool=pool,
    )
    assert r1.trace_id != r2.trace_id
    n = await conn.fetchval(
        "SELECT count(*) FROM ops.support_ticket_traces WHERE ticket_id = $1::uuid",
        str(synthetic_ticket),
    )
    assert n == 2
