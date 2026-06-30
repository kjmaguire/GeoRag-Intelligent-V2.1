"""Phase G.5 — tests for the 5 graduated phase10 support agents.

Each agent's deterministic decision logic gets a pure-function test
plus a DB-roundtrip smoke that requires the live ops.support_tickets
table. The DB smoke skips when POSTGRES_PASSWORD isn't set.
"""
from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from app.agents.context import AgentContext
from app.agents.phase10.customer_response_drafting import (
    _RESPONSE_TEMPLATES,
    customer_response_drafting,
)
from app.agents.phase10.escalation_routing import (
    _ROUTING_TABLE,
    escalation_routing,
)
from app.agents.phase10.root_cause_investigation import (
    root_cause_investigation,
)
from app.agents.phase10.support_packet import support_packet
from app.agents.phase10.ticket_triage import (
    _suggest_category,
    _suggest_severity,
    ticket_triage,
)

# ─────────────────────── ticket_triage helpers ───────────────────────


def test_severity_critical_keyword_trips_critical() -> None:
    sev, evidence = _suggest_severity("the entire platform is down for all users")
    assert sev == "critical"
    assert evidence


def test_severity_high_keyword_trips_high() -> None:
    sev, evidence = _suggest_severity("the dashboard crashed when I clicked export")
    assert sev == "high"


def test_severity_low_keyword_trips_low() -> None:
    sev, evidence = _suggest_severity("typo in the welcome message")
    assert sev == "low"


def test_severity_unknown_defaults_to_medium() -> None:
    sev, evidence = _suggest_severity("hello there how are you")
    assert sev == "medium"
    assert evidence == []


def test_category_wrong_answer_wins() -> None:
    cat, evidence = _suggest_category(
        "the rag hallucinated drill hole IDs that don't exist"
    )
    assert cat == "wrong_answer"
    assert evidence


def test_category_other_when_no_keywords() -> None:
    cat, evidence = _suggest_category("hello there how are you")
    assert cat == "other"
    assert evidence == []


# ─────────────────────── escalation_routing rules ────────────────────


def test_routing_table_covers_all_4_severities() -> None:
    assert set(_ROUTING_TABLE.keys()) == {"critical", "high", "medium", "low"}
    for severity, route in _ROUTING_TABLE.items():
        assert "page" in route
        assert "channel" in route
        assert int(route["sla_minutes"]) > 0


# ─────────────────────── customer_response templates ─────────────────


def test_all_6_categories_have_a_template() -> None:
    # Matches the ops.support_tickets.category CHECK constraint enum.
    expected = {
        "wrong_answer", "failed_ingestion", "failed_report",
        "integration_issue", "performance", "other",
    }
    assert set(_RESPONSE_TEMPLATES.keys()) == expected


def test_response_template_has_resolution_placeholder() -> None:
    for category, template in _RESPONSE_TEMPLATES.items():
        assert "{resolution}" in template, (
            f"template for {category} missing {{resolution}} placeholder"
        )


# ─────────────────────── DB smoke (live container) ───────────────────


def _dsn() -> str:
    return (
        f"postgres://{os.environ.get('POSTGRES_USER', 'georag')}"
        f":{os.environ.get('POSTGRES_PASSWORD', '')}"
        f"@{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}"
        f":{os.environ.get('POSTGRES_DIRECT_PORT', '5432')}"
        f"/{os.environ.get('POSTGRES_DB', 'georag')}"
    )


def _live_db_available() -> bool:
    return bool(os.environ.get("POSTGRES_PASSWORD"))


async def _seed_ticket(
    conn: asyncpg.Connection,
    *,
    description: str,
    severity: str = "medium",
    category: str = "other",
) -> str:
    """Insert one synthetic ticket; return its id.

    channel + category must match the CHECK constraints in
    `database/migrations/2026_05_13_140100_create_ops_support_schema.php`.
    """
    workspace_id = "a0000000-0000-0000-0000-000000000001"
    ticket_id = str(uuid4())
    # Block-3 RLS (2026-05-15): set GUC so WITH CHECK accepts the
    # explicit workspace_id below.
    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
    )
    await conn.execute(
        """
        INSERT INTO ops.support_tickets (
            ticket_id, workspace_id,
            channel, category, severity, description, status
        )
        VALUES ($1::uuid, $2::uuid, 'email', $3, $4, $5, 'open')
        """,
        ticket_id, workspace_id, category, severity, description,
    )
    return ticket_id


async def _drop_ticket(conn: asyncpg.Connection, ticket_id: str) -> None:
    await conn.execute(
        "DELETE FROM ops.support_tickets WHERE ticket_id = $1::uuid",
        ticket_id,
    )


@pytest.fixture
def ctx() -> AgentContext:
    return AgentContext(actor_kind="test")


@pytest.mark.asyncio
async def test_ticket_triage_db_smoke(ctx: AgentContext) -> None:
    """ticket_triage suggests severity + category from a real ticket."""
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        ticket_id = await _seed_ticket(
            conn,
            description="the dashboard crashed when I exported the report",
            severity="low",
            category="other",
        )
        try:
            inner = getattr(ticket_triage, "__wrapped__", ticket_triage)
            result = await inner(ctx, ticket_id=ticket_id)
            assert result["ticket_id"] == ticket_id
            # "crashed" → high; "dashboard" is not a category keyword,
            # so suggested_category falls to "other" — but the
            # suggested_severity should differ from the stored "low".
            assert result["suggested_severity"] == "high"
            assert result["should_change"] is True
        finally:
            await _drop_ticket(conn, ticket_id)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ticket_triage_handles_missing_ticket(ctx: AgentContext) -> None:
    """Missing-ticket path returns a clean error dict.

    The agent invoker requires the @georag_agent runtime to be
    registered (FastAPI lifespan does this in prod). For unit tests
    we invoke the inner function directly via .__wrapped__ to bypass
    the runtime check.
    """
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")
    inner = getattr(ticket_triage, "__wrapped__", ticket_triage)
    result = await inner(ctx, ticket_id=str(uuid4()))
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_support_packet_returns_bundle(ctx: AgentContext) -> None:
    """support_packet returns the ticket + an empty / populated bundle."""
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        ticket_id = await _seed_ticket(conn, description="test ticket")
        try:
            inner = getattr(support_packet, "__wrapped__", support_packet)
            result = await inner(ctx, ticket_id=ticket_id)
            assert result["ticket_id"] == ticket_id
            assert "ticket" in result
            assert "recent_audit_anchors" in result
            assert "recent_workflow_runs" in result
            assert "audit_anchor_count_30d" in result
        finally:
            await _drop_ticket(conn, ticket_id)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_root_cause_investigation_produces_hypothesis(
    ctx: AgentContext,
) -> None:
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        ticket_id = await _seed_ticket(
            conn, description="answer was wrong", category="wrong_answer",
        )
        try:
            inner = getattr(
                root_cause_investigation, "__wrapped__", root_cause_investigation
            )
            result = await inner(ctx, ticket_id=ticket_id)
            assert result["ticket_id"] == ticket_id
            assert "hypothesis" in result
            assert result["confidence"] in {"low", "medium", "high"}
            assert "supporting_signals" in result
            assert "similar_recent_tickets" in result
        finally:
            await _drop_ticket(conn, ticket_id)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_customer_response_drafting_renders_template(
    ctx: AgentContext,
) -> None:
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        ticket_id = await _seed_ticket(conn, description="x", category="wrong_answer")
        try:
            inner = getattr(
                customer_response_drafting, "__wrapped__", customer_response_drafting
            )
            result = await inner(
                ctx,
                ticket_id=ticket_id,
                resolution_summary="adjusted the retrieval ranking",
            )
            assert result["ticket_id"] == ticket_id
            assert result["category_used"] == "wrong_answer"
            assert "adjusted the retrieval ranking" in result["draft_response"]
            assert result["ready_to_send"] is False
        finally:
            await _drop_ticket(conn, ticket_id)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_escalation_routing_recommends_per_severity(
    ctx: AgentContext,
) -> None:
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        ticket_id = await _seed_ticket(
            conn, description="urgent", severity="critical",
        )
        try:
            inner = getattr(escalation_routing, "__wrapped__", escalation_routing)
            result = await inner(ctx, ticket_id=ticket_id)
            assert result["ticket_id"] == ticket_id
            assert result["severity"] == "critical"
            assert result["route_to"] == "primary_on_call"
            assert result["sla_minutes"] == 15
            assert result["applied"] is False
        finally:
            await _drop_ticket(conn, ticket_id)
    finally:
        await conn.close()
