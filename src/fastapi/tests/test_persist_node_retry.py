"""Unit tests for persist_node retry-with-backoff + terminal escalation.

Background — prior to this change, ``persist_node`` wrapped the
``silver.answer_runs`` INSERT in a single ``try / except Exception``
that swallowed any DB failure with ``logger.exception(...)``. A
transient asyncpg flap (PgBouncer saturation, brief network blip, PG
restart) therefore permanently lost the lineage row with no
operator-facing signal.

The retrofit now wraps the INSERT in 3-attempt exponential backoff via
``_insert_answer_run_with_retry`` (0.5s → 1.0s → 2.0s). On terminal
failure the node:

  * Logs at ``logger.error`` with ``extra={"alert": True}`` for
    Alertmanager pickup.
  * Increments the Prometheus counter
    ``georag_agentic_persist_failures_total{stage="answer_runs"}``.

These tests pin the retry contract without needing a live PG. The
``pg_pool`` is mocked so we control how many attempts fail before the
INSERT succeeds (or doesn't).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.agent.agentic_retrieval.nodes import persist_node
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.metrics import AGENTIC_PERSIST_FAILURES
from app.models.rag import Citation, GeoRAGResponse

TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _DepsStub:
    """Minimum surface persist_node needs from the AgentDeps bundle."""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool
        self.project_id: str | None = None
        self.workspace_id: str | None = TEST_WORKSPACE_ID


def _make_response() -> GeoRAGResponse:
    return GeoRAGResponse(
        text="Hole 36-1085 cuts the Roll-Front sandstone.",
        citations=[
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="retry-test-chunk",
                document_title="Persist node retry unit test",
                relevance_score=0.9,
            )
        ],
        confidence=0.9,
        sources_used=["retry-test-chunk"],
    )


def _build_state() -> AgenticRetrievalState:
    return AgenticRetrievalState(
        query="tell me about hole 36-1085",
        deps=_DepsStub(pg_pool=None),  # pool is patched below
        intent="factual_lookup",
        effective_intent="factual_lookup",
        response=_make_response(),
        run_start_monotonic=time.monotonic(),
    )


def _make_mock_pool(side_effects: list[Any]) -> MagicMock:
    """Build a MagicMock pg_pool whose acquire().__aenter__() returns a
    connection whose fetchrow follows ``side_effects`` in order.

    Each side effect is either an Exception (to raise) or a row dict
    (to return).
    """
    conn = MagicMock()

    fetchrow = AsyncMock(side_effect=side_effects)
    conn.fetchrow = fetchrow
    # the bare execute path used by child-row writers — must succeed
    # silently so the test focuses on the answer_runs INSERT.
    conn.execute = AsyncMock(return_value="INSERT 0 0")

    @asynccontextmanager
    async def _acquire() -> Any:
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    # Carry the conn handle on the pool so the test can assert call count.
    pool._fetchrow = fetchrow
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _zero_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real exponential backoff sleeps so the test stays fast.

    persist_node imports ``asyncio`` inline inside
    ``_insert_answer_run_with_retry``; patching ``asyncio.sleep`` on the
    canonical module covers the inline import.
    """
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


@pytest.mark.asyncio
async def test_persist_node_succeeds_after_two_transient_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two transient asyncpg flaps → the 3rd attempt succeeds.

    Metric must NOT increment. The eventually-successful row id must
    be stamped onto state.response.answer_run_id.
    """
    success_row = {"answer_run_id": uuid4()}
    pool = _make_mock_pool(
        side_effects=[
            ConnectionError("PgBouncer dropped (1/3)"),
            ConnectionError("PgBouncer dropped (2/3)"),
            success_row,
        ]
    )

    state = _build_state()
    state.deps.pg_pool = pool

    # Sample the counter before/after — Prometheus Counters expose
    # the current value through their internal `_value.get()` accessor.
    before = AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs")._value.get()

    caplog.set_level(logging.WARNING, logger="app.agent.agentic_retrieval.nodes")
    update = await persist_node(state)

    after = AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs")._value.get()

    assert pool._fetchrow.await_count == 3, (
        "Expected 3 fetchrow attempts (2 failures + 1 success) — "
        f"got {pool._fetchrow.await_count}."
    )
    assert after == before, (
        "Counter must NOT increment when the retry loop eventually "
        f"succeeds. before={before} after={after}"
    )
    assert "response" in update
    assert state.response.answer_run_id == UUID(str(success_row["answer_run_id"]))

    # The intermediate failures should surface as warnings (not errors).
    warning_messages = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any("attempt 1/3" in m for m in warning_messages), (
        "Expected a 'attempt 1/3 failed' warning for the first transient "
        "error — got: " + repr(warning_messages)
    )
    assert any("attempt 2/3" in m for m in warning_messages), (
        "Expected a 'attempt 2/3 failed' warning for the second transient "
        "error — got: " + repr(warning_messages)
    )


@pytest.mark.asyncio
async def test_persist_node_escalates_when_all_retries_exhausted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All 3 attempts fail → metric +=1 AND logger.error called.

    The node must still return (non-fatal) so the streamed answer
    isn't disrupted.
    """
    pool = _make_mock_pool(
        side_effects=[
            ConnectionError("PG restart (1/3)"),
            ConnectionError("PG restart (2/3)"),
            ConnectionError("PG restart (3/3)"),
        ]
    )

    state = _build_state()
    state.deps.pg_pool = pool

    before = AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs")._value.get()

    caplog.set_level(logging.DEBUG, logger="app.agent.agentic_retrieval.nodes")
    update = await persist_node(state)

    after = AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs")._value.get()

    assert pool._fetchrow.await_count == 3, (
        "Expected exactly 3 fetchrow attempts before terminal escalation — "
        f"got {pool._fetchrow.await_count}."
    )
    assert after == before + 1, (
        "AGENTIC_PERSIST_FAILURES must increment exactly once on terminal "
        f"failure. before={before} after={after}"
    )

    # The terminal log line must be at ERROR level (not exception/warning)
    # so Alertmanager's level=error matcher picks it up. extra={"alert":
    # True} stays on the LogRecord as an attribute.
    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and "answer_runs INSERT failed after retries" in r.getMessage()
    ]
    assert error_records, (
        "Expected exactly one ERROR log carrying the terminal-failure "
        "message — got: "
        + repr([(r.levelno, r.getMessage()) for r in caplog.records])
    )
    assert getattr(error_records[0], "alert", None) is True, (
        "The terminal log must carry extra={'alert': True} so "
        "Alertmanager can route it."
    )

    # persist_node returns the response (with answer_run_id still None)
    # so LangGraph propagates downstream without crashing the answer
    # path.
    assert "response" in update
    assert state.response.answer_run_id is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
