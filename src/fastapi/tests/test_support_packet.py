"""Live tests for the §25.4 support_packet agent (doc-phase 140)."""
from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from app.services.support_cockpit.root_cause_investigation import (
    investigate_ticket,
)
from app.services.support_cockpit.support_packet import build_support_packet
from app.services.support_cockpit.ticket_triage import triage_ticket


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
    email = f"test-packet-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        """
        INSERT INTO public.users (name, email, password)
        VALUES ($1, $2, $3) RETURNING id
        """,
        "Packet test user", email, "test-hash",
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
            'other', $2, 'medium', 'open'
        )
        RETURNING ticket_id
        """,
        synthetic_user,
        f"[{prefix}] packet test ticket — system returned wrong answer about Cu",
    )
    try:
        yield tid
    finally:
        await conn.execute(
            "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid",
            str(tid),
        )


# ----------------------------------------------------------------------
# End-to-end DB tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_build_support_packet_includes_ticket_info(
    pool, synthetic_ticket, synthetic_user
):
    """Bare-minimum packet (just ticket info, no triage/investigation
    yet) builds successfully and includes the ticket fields."""
    packet = await build_support_packet(ticket_id=synthetic_ticket, pool=pool)
    assert packet.ticket_id is not None
    assert packet.ticket["category"] == "other"
    assert packet.ticket["severity"] == "medium"
    assert packet.ticket["status"] == "open"
    # No triage/investigation yet → empty lists.
    assert packet.triage_anchors == []
    assert packet.investigation_anchors == []
    assert packet.packet_anchor_id is not None


@pytest.mark.asyncio
async def test_build_support_packet_includes_triage_chain(
    pool, conn, synthetic_ticket, synthetic_user
):
    """After triage + investigation, packet captures the full chain."""
    # Triage first.
    await triage_ticket(ticket_id=synthetic_ticket, pool=pool)
    # Investigate.
    await investigate_ticket(
        ticket_id=synthetic_ticket,
        actor_user_id=synthetic_user,
        pool=pool,
    )

    packet = await build_support_packet(ticket_id=synthetic_ticket, pool=pool)

    # Chain visible.
    assert len(packet.triage_anchors) == 1
    assert len(packet.investigation_anchors) == 1
    assert len(packet.trace_links) == 1
    # Ticket reflects the triage update.
    assert packet.ticket["status"] == "investigating"
    # Investigation anchor carries the trace_id.
    inv = packet.investigation_anchors[0]
    assert inv.get("trace_id", "").startswith("inv_")

    # Audit anchor for the packet itself lands.
    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE id = $1::uuid
           AND action_type = 'support.packet.assembled'
        """,
        str(packet.packet_anchor_id),
    )
    assert n == 1


@pytest.mark.asyncio
async def test_build_support_packet_unknown_id_raises(pool):
    with pytest.raises(ValueError, match="not found"):
        await build_support_packet(ticket_id=uuid4(), pool=pool)


@pytest.mark.asyncio
async def test_build_support_packet_summary_format(pool, synthetic_ticket):
    packet = await build_support_packet(ticket_id=synthetic_ticket, pool=pool)
    # Summary mentions the category + severity + counts.
    assert "category=other" in packet.summary
    assert "severity=medium" in packet.summary
    assert "0 triage" in packet.summary
    assert "0 investigation" in packet.summary


@pytest.mark.asyncio
async def test_build_support_packet_multiple_calls_emit_distinct_anchors(
    pool, conn, synthetic_ticket
):
    """Each packet assembly emits its own audit anchor — packets are
    not deduped (they're snapshots in time)."""
    p1 = await build_support_packet(ticket_id=synthetic_ticket, pool=pool)
    p2 = await build_support_packet(ticket_id=synthetic_ticket, pool=pool)
    assert p1.packet_anchor_id != p2.packet_anchor_id

    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'support.packet.assembled'
           AND target_id = $1
        """,
        str(synthetic_ticket),
    )
    assert n >= 2
