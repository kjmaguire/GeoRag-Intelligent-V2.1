"""L1546 — execute the usage.usage_events INSERT against a real Postgres.

The unit tests in test_llm_spend_accounting.py mock the connection, so
they prove the call shape and the pricing gate but cannot catch a column
name that does not exist, a type that will not cast, or a CHECK constraint
the chosen `outcome` value violates. Those are exactly the failures that
would leave the meter silently broken again — `_write_chat_usage_event`
swallows its own exceptions on purpose, because the answer has already
been streamed to the user by the time it runs.

So this file runs the real statement. Gated on the `integration` marker
like the rest of the DB-backed suite.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from app.agent.agentic_retrieval.nodes import _write_chat_usage_event
from app.db.dsn import build_dsn

pytestmark_integration = pytest.mark.integration

TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(
        build_dsn(direct=True, scheme="postgresql"), min_size=1, max_size=2
    )
    try:
        yield pool
    finally:
        await pool.close()


async def _rows_for(pool, trace_id: str) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM usage.usage_events WHERE trace_id = $1", trace_id
        )


async def _cleanup(pool, trace_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM usage.usage_events WHERE trace_id = $1", trace_id
        )


@pytestmark_integration
@pytest.mark.asyncio
async def test_the_usage_row_actually_lands(pg_pool):
    trace_id = uuid.uuid4().hex
    run_id = str(uuid.uuid4())

    await _write_chat_usage_event(
        pg_pool,
        workspace_id=TEST_WORKSPACE_ID,
        model_id="Cohere-command-a-plus-05-2026",
        backend="azure",
        input_tokens=12_345,
        output_tokens=678,
        latency_ms=4_200,
        trace_id=trace_id,
        answer_run_id=run_id,
    )

    try:
        rows = await _rows_for(pg_pool, trace_id)
        assert len(rows) == 1, (
            "No row landed. _write_chat_usage_event swallows its own "
            "exceptions — check the warning it logs."
        )
        row = rows[0]
        assert row["agent_name"] == "chat_rag"
        assert row["model_profile"] == "azure"
        assert row["model_id"] == "Cohere-command-a-plus-05-2026"
        assert row["tokens_prompt"] == 12_345
        assert row["tokens_completion"] == 678
        # Generated column — proves the two counts are stored as the
        # schema expects rather than swapped or stringified.
        assert row["tokens_total"] == 13_023
        assert float(row["projected_cost_usd"]) == 0.0
        assert row["latency_ms"] == 4_200
        assert row["outcome"] == "success"
        assert str(row["invocation_id"]) == run_id
    finally:
        await _cleanup(pg_pool, trace_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_the_outcome_value_satisfies_the_check_constraint(pg_pool):
    # usage_events.outcome has a CHECK over
    # ('success','refusal','failure','timeout','circuit_open'). A value
    # outside it would raise inside the swallowed try and the meter would
    # be silently dead — the exact failure mode this finding is about.
    trace_id = uuid.uuid4().hex
    async with pg_pool.acquire() as conn:
        constraint = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'usage.usage_events'::regclass
               AND pg_get_constraintdef(oid) ILIKE '%outcome%'
             LIMIT 1
            """
        )
    assert constraint is not None
    assert "'success'" in constraint

    await _write_chat_usage_event(
        pg_pool,
        workspace_id=TEST_WORKSPACE_ID,
        model_id="claude-sonnet-4-6",
        backend="anthropic",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        trace_id=trace_id,
        answer_run_id=None,
    )
    try:
        assert len(await _rows_for(pg_pool, trace_id)) == 1
    finally:
        await _cleanup(pg_pool, trace_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_a_null_workspace_does_not_break_the_write(pg_pool):
    # System-level calls are not workspace-scoped. cost_burn_watcher's
    # query already filters `workspace_id IS NOT NULL`, so these rows are
    # inert for the burn sum but still count toward total spend.
    trace_id = uuid.uuid4().hex

    await _write_chat_usage_event(
        pg_pool,
        workspace_id=None,
        model_id="claude-sonnet-4-6",
        backend="anthropic",
        input_tokens=10,
        output_tokens=5,
        latency_ms=None,
        trace_id=trace_id,
        answer_run_id=None,
    )
    try:
        rows = await _rows_for(pg_pool, trace_id)
        assert len(rows) == 1
        assert rows[0]["workspace_id"] is None
        assert rows[0]["latency_ms"] is None
    finally:
        await _cleanup(pg_pool, trace_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_a_priced_model_records_a_real_cost(pg_pool):
    trace_id = uuid.uuid4().hex

    await _write_chat_usage_event(
        pg_pool,
        workspace_id=TEST_WORKSPACE_ID,
        model_id="claude-sonnet-4-6",
        backend="anthropic",
        input_tokens=1_000_000,
        output_tokens=0,
        latency_ms=100,
        trace_id=trace_id,
        answer_run_id=None,
    )
    try:
        rows = await _rows_for(pg_pool, trace_id)
        assert len(rows) == 1
        # NUMERIC(12,6) — must survive the round trip at full precision.
        assert float(rows[0]["projected_cost_usd"]) == pytest.approx(3.00)
    finally:
        await _cleanup(pg_pool, trace_id)
