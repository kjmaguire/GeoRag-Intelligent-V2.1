"""Live tests for the §25.4 ticket_triage agent (doc-phase 136).

Verifies:
  - Classifier maps representative descriptions to expected severities
    + categories
  - triage_ticket writes the new classification + transitions
    status='open' → 'investigating'
  - Audit anchor (support.ticket.triaged) emitted on completion
  - triage_unclassified_tickets picks up all open tickets in one pass
"""
from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.services.support_cockpit.ticket_triage import (
    VALID_CATEGORIES,
    VALID_SEVERITIES,
    _synthetic_classifier,
    triage_ticket,
    triage_unclassified_tickets,
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
    # Block-3 RLS (2026-05-15): ops.support_tickets enforces strict
    # workspace_id RLS. Default to the Default Workspace so test
    # fixtures don't have to thread workspace_id through every INSERT.
    await c.execute(
        "SELECT set_config('app.workspace_id', $1, false)",
        "a0000000-0000-0000-0000-000000000001",
    )
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_ticket(conn):
    """Insert + tear down a synthetic ops.support_tickets row."""
    prefix = uuid4().hex[:8]
    ticket_id = await conn.fetchval(
        """
        INSERT INTO ops.support_tickets (
            channel, category, description, severity, status
        )
        VALUES ('in_app', 'other', $1, 'medium', 'open')
        RETURNING ticket_id
        """,
        f"[{prefix}] Synthetic ticket: the system gave a wrong answer about uranium occurrences.",
    )
    try:
        yield ticket_id
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid",
            str(ticket_id),
        )


# ----------------------------------------------------------------------
# Classifier unit tests (no DB)
# ----------------------------------------------------------------------
def test_classifier_critical_for_crash():
    sev, cat = _synthetic_classifier(
        "the app crashed when I uploaded a 50 MB PDF"
    )
    assert sev == "critical"
    assert cat == "failed_ingestion"
    assert sev in VALID_SEVERITIES
    assert cat in VALID_CATEGORIES


def test_classifier_high_for_wrong_answer():
    sev, cat = _synthetic_classifier(
        "The chat answer was wrong — it said gold when the assays show copper."
    )
    assert sev == "high"
    assert cat == "wrong_answer"


def test_classifier_medium_for_slow():
    sev, cat = _synthetic_classifier(
        "Search has been slow today — taking 8+ seconds per query."
    )
    assert sev == "medium"
    assert cat == "performance"


def test_classifier_default_low_other():
    sev, cat = _synthetic_classifier("Just sharing some general feedback.")
    assert sev == "low"
    assert cat == "other"


def test_classifier_failed_report():
    sev, cat = _synthetic_classifier("Report export to docx failed midway.")
    assert sev == "critical"
    assert cat == "failed_report"


def test_classifier_integration_issue():
    sev, cat = _synthetic_classifier(
        "Activepieces webhook keeps returning a 502 — integration is broken."
    )
    assert sev == "critical"
    assert cat == "integration_issue"


# ----------------------------------------------------------------------
# End-to-end DB tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_triage_ticket_end_to_end(pool, conn, synthetic_ticket):
    """Smoke: triage one ticket → state changes + audit anchor."""
    outcome = await triage_ticket(ticket_id=synthetic_ticket, pool=pool)

    assert outcome.ticket_id == UUID(str(synthetic_ticket))
    assert outcome.new_status == "investigating"
    assert outcome.new_severity == "high"  # 'wrong answer' → high
    assert outcome.new_category == "wrong_answer"
    assert outcome.triage_method == "synthetic_stub"

    # Row state matches.
    row = await conn.fetchrow(
        """
        SELECT severity, category, status FROM ops.support_tickets
         WHERE ticket_id = $1::uuid
        """,
        str(synthetic_ticket),
    )
    assert row["severity"] == "high"
    assert row["category"] == "wrong_answer"
    assert row["status"] == "investigating"

    # Audit anchor lands.
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'support.ticket.triaged'
           AND target_id = $1
        """,
        str(synthetic_ticket),
    )
    assert n == 1


@pytest.mark.asyncio
async def test_triage_ticket_rejects_closed_ticket(pool, conn):
    """Cannot re-triage a closed ticket."""
    ticket_id = await conn.fetchval(
        """
        INSERT INTO ops.support_tickets (channel, category, description, severity, status)
        VALUES ('in_app', 'other', 'closed test', 'low', 'closed')
        RETURNING ticket_id
        """
    )
    try:
        with pytest.raises(ValueError, match="closed"):
            await triage_ticket(ticket_id=ticket_id, pool=pool)
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid",
            str(ticket_id),
        )


@pytest.mark.asyncio
async def test_triage_ticket_unknown_id_raises(pool):
    """Nonexistent ticket → ValueError."""
    with pytest.raises(ValueError, match="not found"):
        await triage_ticket(ticket_id=uuid4(), pool=pool)


@pytest.mark.asyncio
async def test_triage_unclassified_tickets_bulk(pool, conn):
    """Insert 3 open tickets → bulk triage → all 3 transition."""
    prefix = uuid4().hex[:8]
    ticket_ids: list[str] = []
    descriptions = [
        f"[{prefix}] My PDF upload crashed",
        f"[{prefix}] Wrong answer about uranium",
        f"[{prefix}] Page loads are slow",
    ]
    for desc in descriptions:
        tid = await conn.fetchval(
            """
            INSERT INTO ops.support_tickets (channel, category, description, severity, status)
            VALUES ('in_app', 'other', $1, 'medium', 'open')
            RETURNING ticket_id
            """,
            desc,
        )
        ticket_ids.append(str(tid))

    try:
        outcomes = await triage_unclassified_tickets(limit=10, pool=pool)
        # Our 3 should be in there (plus any other open tickets).
        triaged_ids = {str(o.ticket_id) for o in outcomes}
        for tid in ticket_ids:
            assert tid in triaged_ids

        # All three transitioned to investigating.
        states = await conn.fetch(
            "SELECT status FROM ops.support_tickets WHERE ticket_id = ANY($1::uuid[])",
            ticket_ids,
        )
        for r in states:
            assert r["status"] == "investigating"
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = ANY($1::uuid[])",
            ticket_ids,
        )
