"""RetrievalInspector follow-up — agentic-retrieval persist_node coverage.

Background — the deep-link inspector failure that re-surfaced on a fresh
chat message was because `AGENTIC_RETRIEVAL_V2_ENABLED=true` dispatches
the chat to `run_agentic_retrieval`, NOT `run_deterministic_rag`. The
agentic graph has its own `persist_node` that writes its own row into
`silver.answer_runs`, and (prior to this commit) it:

  • used `await conn.execute(...)` with no `RETURNING` clause, so the
    generated `answer_run_id` was discarded
  • never stamped that id back onto `state.response.answer_run_id`, so
    the SSE `completed` frame still carried the streaming-session UUID
  • didn't populate `confidence` or `latency_ms`, so the inspector page
    header always read "conf — · —ms"

These tests pin the fixed contract:

  1. The state model carries `run_start_monotonic` so persist_node can
     compute wall-clock latency without re-measuring inside the node.
  2. persist_node round-trips `confidence` + `latency_ms` to the row.
  3. persist_node stamps `state.response.answer_run_id` from the
     `RETURNING` clause so the Retrieval Inspector deep link resolves.
  4. The LangGraph propagates that mutation back to the caller.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from app.agent.agentic_retrieval.nodes import persist_node
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.models.rag import Citation, GeoRAGResponse

PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)
TEST_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Pure model coverage (no DB) — locks the state contract
# ---------------------------------------------------------------------------


class TestStateRunStartMonotonic:
    """The latency-clock field carried through the LangGraph state."""

    def test_default_is_none(self) -> None:
        state = AgenticRetrievalState(query="hi", deps=object())
        assert state.run_start_monotonic is None

    def test_accepts_monotonic_float(self) -> None:
        start = time.monotonic()
        state = AgenticRetrievalState(
            query="hi", deps=object(), run_start_monotonic=start
        )
        assert state.run_start_monotonic == start


# ---------------------------------------------------------------------------
# Integration coverage against live PG
# ---------------------------------------------------------------------------


pytestmark_integration = pytest.mark.integration


@dataclass
class _DepsStub:
    """Minimum surface persist_node needs from the AgentDeps bundle."""

    pg_pool: Any
    project_id: str | None = None
    workspace_id: str | None = None


def _make_response(text: str, *, confidence: float) -> GeoRAGResponse:
    return GeoRAGResponse(
        text=text,
        citations=[
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="agentic-persist-test",
                document_title="Persist node integration test",
                relevance_score=confidence,
            )
        ],
        confidence=confidence,
        sources_used=["agentic-persist-test"],
    )


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


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
async def test_persist_node_stamps_answer_run_id_on_response(pg_pool):
    """persist_node must surface the DB row id on state.response."""
    response = _make_response(
        "Hole 36-1085 cuts the Roll-Front sandstone.", confidence=0.91
    )
    state = AgenticRetrievalState(
        query="tell me about hole 36-1085",
        deps=_DepsStub(pg_pool=pg_pool, workspace_id=str(TEST_WORKSPACE_ID)),
        intent="factual_lookup",
        effective_intent="factual_lookup",
        response=response,
        run_start_monotonic=time.monotonic(),
    )

    update = await persist_node(state)

    # persist_node returns the (mutated) response so LangGraph propagates
    # the stamp forward through the state merge.
    assert "response" in update
    assert update["response"] is response

    assert response.answer_run_id is not None, (
        "Regression: persist_node did not stamp answer_run_id — the "
        "Retrieval Inspector deep link will resolve to nothing."
    )

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT confidence::float8 AS confidence, latency_ms, "
                "       query_text, citation_lifecycle_state "
                "  FROM silver.answer_runs WHERE answer_run_id = $1::uuid",
                str(response.answer_run_id),
            )
        assert row is not None
        assert row["query_text"] == "tell me about hole 36-1085"
        # citations non-empty + valid → 'committed' lifecycle
        assert row["citation_lifecycle_state"] == "committed"
    finally:
        await _cleanup(pg_pool, response.answer_run_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_persist_node_populates_confidence_and_latency(pg_pool):
    """Both new inspector columns must be written by the agentic path."""
    response = _make_response("Mineralisation extends 80m below collar.", confidence=0.62)
    start = time.monotonic()
    state = AgenticRetrievalState(
        query="how deep does the mineralisation go",
        deps=_DepsStub(pg_pool=pg_pool, workspace_id=str(TEST_WORKSPACE_ID)),
        intent="synthesis",
        effective_intent="synthesis",
        response=response,
        run_start_monotonic=start,
    )

    # Force a measurable elapsed window so latency_ms > 0 and we know the
    # node actually computed the delta (not a hardcoded zero).
    import asyncio
    await asyncio.sleep(0.02)

    await persist_node(state)

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT confidence::float8 AS confidence, latency_ms "
                "  FROM silver.answer_runs WHERE answer_run_id = $1::uuid",
                str(response.answer_run_id),
            )
        assert row["confidence"] == pytest.approx(0.62, abs=1e-4)
        assert row["latency_ms"] is not None
        assert row["latency_ms"] >= 20, (
            f"latency_ms={row['latency_ms']} — expected ≥ 20ms after the "
            "20ms asyncio.sleep above; persist_node may have ignored "
            "state.run_start_monotonic."
        )
    finally:
        await _cleanup(pg_pool, response.answer_run_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_persist_node_writes_null_latency_when_clock_missing(pg_pool):
    """Defensive — direct-driving the node without run_start_monotonic
    (the test-suite path) must NULL latency_ms rather than crash."""
    response = _make_response("ok", confidence=0.5)
    state = AgenticRetrievalState(
        query="latency clock missing",
        deps=_DepsStub(pg_pool=pg_pool, workspace_id=str(TEST_WORKSPACE_ID)),
        intent="factual_lookup",
        effective_intent="factual_lookup",
        response=response,
        run_start_monotonic=None,
    )

    await persist_node(state)

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT latency_ms FROM silver.answer_runs "
                " WHERE answer_run_id = $1::uuid",
                str(response.answer_run_id),
            )
        assert row["latency_ms"] is None
    finally:
        await _cleanup(pg_pool, response.answer_run_id)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
