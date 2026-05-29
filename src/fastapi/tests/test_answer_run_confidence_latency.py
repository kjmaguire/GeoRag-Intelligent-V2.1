"""RetrievalInspector follow-up — confidence + latency_ms + refusal rows.

Three layers of coverage:

1. Pydantic model: AnswerRunCreate accepts (and validates) the new
   `confidence`, `latency_ms`, and `rejection_reason` fields.

2. Integration (real PG): insert_answer_run round-trips the new columns;
   the CHECK constraints enforce 0 ≤ confidence ≤ 1 and latency_ms ≥ 0.

3. Integration (real PG): insert_refusal_answer_run writes a
   citation_lifecycle_state='rejected' row that the Retrieval Inspector
   controller can deep-link to with a sensible rejection_reason.

Run with `pytest tests/test_answer_run_confidence_latency.py`. Integration
tests are gated on the `integration` marker so the unit-only CI lane
skips them; the inner-loop dev environment runs them by default.
"""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import asyncpg
import pytest

from app.models.answer_run import AnswerRunCreate
from app.services.answer_run_store import (
    insert_answer_run,
    insert_refusal_answer_run,
)


PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)
TEST_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# 1. Pure model tests — no DB
# ---------------------------------------------------------------------------


class TestAnswerRunCreateNewFields:
    """confidence + latency_ms + rejection_reason validation."""

    def _base_kwargs(self) -> dict:
        return {
            "workspace_id": TEST_WORKSPACE_ID,
            "query_text": "tell me about hole 36-1085",
            "query_class": "factual",
            "workspace_data_version_at_query": 1,
        }

    def test_fields_default_to_none(self) -> None:
        run = AnswerRunCreate(**self._base_kwargs())
        assert run.confidence is None
        assert run.latency_ms is None
        assert run.rejection_reason is None

    def test_confidence_accepts_zero_and_one(self) -> None:
        AnswerRunCreate(**self._base_kwargs(), confidence=0.0)
        AnswerRunCreate(**self._base_kwargs(), confidence=1.0)
        AnswerRunCreate(**self._base_kwargs(), confidence=0.873)

    def test_confidence_rejects_above_one(self) -> None:
        with pytest.raises(Exception):
            AnswerRunCreate(**self._base_kwargs(), confidence=1.001)

    def test_confidence_rejects_negative(self) -> None:
        with pytest.raises(Exception):
            AnswerRunCreate(**self._base_kwargs(), confidence=-0.0001)

    def test_latency_ms_accepts_zero_and_positive(self) -> None:
        AnswerRunCreate(**self._base_kwargs(), latency_ms=0)
        AnswerRunCreate(**self._base_kwargs(), latency_ms=12345)

    def test_latency_ms_rejects_negative(self) -> None:
        with pytest.raises(Exception):
            AnswerRunCreate(**self._base_kwargs(), latency_ms=-1)

    def test_rejection_reason_accepts_free_text(self) -> None:
        run = AnswerRunCreate(
            **self._base_kwargs(),
            rejection_reason="llm_unavailable",
        )
        assert run.rejection_reason == "llm_unavailable"


# ---------------------------------------------------------------------------
# 2 + 3. Integration tests against real PG
# ---------------------------------------------------------------------------


pytestmark_integration = pytest.mark.integration


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


async def _fetch_row(pool, run_id: UUID) -> asyncpg.Record:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT confidence::float8 AS confidence, latency_ms, "
            "       citation_lifecycle_state, rejection_reason, query_text "
            "  FROM silver.answer_runs "
            " WHERE answer_run_id = $1::uuid",
            str(run_id),
        )


async def _cleanup(pool, run_id: UUID | None) -> None:
    if run_id is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM silver.answer_runs WHERE answer_run_id = $1::uuid",
            str(run_id),
        )


@pytestmark_integration
@pytest.mark.asyncio
async def test_insert_persists_confidence_and_latency_ms(pg_pool):
    """insert_answer_run round-trips the new columns."""
    run = AnswerRunCreate(
        workspace_id=TEST_WORKSPACE_ID,
        query_text="integration: confidence + latency round-trip",
        query_class="factual",
        workspace_data_version_at_query=1,
        confidence=0.873,
        latency_ms=4321,
        citation_lifecycle_state="committed",
    )
    run_id = await insert_answer_run(pg_pool, run)
    try:
        assert run_id is not None
        row = await _fetch_row(pg_pool, run_id)
        assert row is not None
        assert row["confidence"] == pytest.approx(0.873, abs=1e-4)
        assert row["latency_ms"] == 4321
    finally:
        await _cleanup(pg_pool, run_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_confidence_check_constraint_blocks_over_one(pg_pool):
    """The CHECK constraint catches drift even if Pydantic is bypassed.

    Hits the column directly via raw SQL so the test asserts the DB
    enforces the invariant. Belt-and-suspenders: Pydantic + DB CHECK.
    """
    bad_id = uuid.uuid4()
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO silver.answer_runs ("
                "  answer_run_id, workspace_id, query_text, query_class, "
                "  workspace_data_version_at_query, confidence"
                ") VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)",
                str(bad_id),
                str(TEST_WORKSPACE_ID),
                "constraint test",
                "factual",
                1,
                1.5,
            )


@pytestmark_integration
@pytest.mark.asyncio
async def test_latency_ms_check_constraint_blocks_negative(pg_pool):
    bad_id = uuid.uuid4()
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO silver.answer_runs ("
                "  answer_run_id, workspace_id, query_text, query_class, "
                "  workspace_data_version_at_query, latency_ms"
                ") VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)",
                str(bad_id),
                str(TEST_WORKSPACE_ID),
                "constraint test",
                "factual",
                1,
                -5,
            )


@pytestmark_integration
@pytest.mark.asyncio
async def test_refusal_helper_writes_inspectable_row(pg_pool):
    """insert_refusal_answer_run produces a row the Inspector can show."""
    run_id = await insert_refusal_answer_run(
        pg_pool,
        workspace_id=TEST_WORKSPACE_ID,
        project_id=None,
        query_text="what's the weather in Tokyo",
        rejection_reason="out_of_scope",
        latency_ms=87,
    )
    try:
        assert run_id is not None
        row = await _fetch_row(pg_pool, run_id)
        assert row is not None
        assert row["citation_lifecycle_state"] == "rejected"
        assert row["rejection_reason"] == "out_of_scope"
        assert row["confidence"] == pytest.approx(0.0)
        assert row["latency_ms"] == 87
        assert row["query_text"] == "what's the weather in Tokyo"
    finally:
        await _cleanup(pg_pool, run_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_refusal_helper_skips_when_pool_none() -> None:
    """Defensive: pool=None must not raise — observability is fire-and-forget."""
    result = await insert_refusal_answer_run(
        None,
        workspace_id=TEST_WORKSPACE_ID,
        project_id=None,
        query_text="…",
        rejection_reason="llm_unavailable",
        latency_ms=0,
    )
    assert result is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
