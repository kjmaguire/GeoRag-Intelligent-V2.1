"""Audit 2026-09-29, batch B: persist_node on the path to `completed`.

AGT-6   bounded wall-clock, transient-only retries, batched child rows.
AGT-17  user-visible stamps and the trace survive an INSERT failure;
        per-source counts read dataclass results.
AGT-1   answer_runs.query_text is what the user asked, not the rewrite.
RAG-22  citation_mode is written (always posthoc_span_resolution).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import (
    _ANSWER_RUN_INSERT_SQL,
    _batched_executemany,
    _batched_retrieval_insert,
    _result_row_count,
    persist_node,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.tools import (
    AssayDataResult,
    DocumentChunk,
    DocumentSearchResult,
)
from app.metrics import AGENTIC_PERSIST_FAILURES
from app.models.rag import Citation, GeoRAGResponse

WS = "a0000000-0000-0000-0000-000000000001"


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool
        self.project_id = None
        self.workspace_id = WS


def _response() -> GeoRAGResponse:
    return GeoRAGResponse(
        text="Hole 36-1085 cuts the sandstone [DATA-1].",
        citations=[Citation(
            citation_id="[DATA-1]", citation_type="DATA",
            source_chunk_id="chunk-1", document_title="T", relevance_score=0.9,
        )],
        confidence=0.9,
        sources_used=["chunk-1"],
    )


def _pool(fetchrow: Any) -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = fetchrow
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchval = AsyncMock(return_value=1)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool.conn = conn
    return pool


def _state(pool: Any, **extra: Any) -> AgenticRetrievalState:
    fields: dict[str, Any] = {
        "query": "tell me about hole 36-1085", "deps": _Deps(pool),
        "intent": "factual_lookup", "effective_intent": "factual_lookup",
        "response": _response(), "run_start_monotonic": time.monotonic(),
    }
    fields.update(extra)
    return AgenticRetrievalState(**fields)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _sleep(_d: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _sleep)


@pytest.fixture
def traces(monkeypatch):
    captured: list[Any] = []

    async def _enqueue(_pool, trace):
        captured.append(trace)

    import app.services.trace_writer as _tw

    monkeypatch.setattr(_tw, "enqueue_trace", _enqueue)
    return captured


# ---------------------------------------------------------------------------
# AGT-6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_error_is_not_retried(traces):
    fetchrow = AsyncMock(side_effect=asyncpg.exceptions.CheckViolationError("bad"))
    pool = _pool(fetchrow)
    before = AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs")._value.get()
    await persist_node(_state(pool))
    assert fetchrow.await_count == 1
    assert AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs")._value.get() == before + 1


@pytest.mark.asyncio
async def test_hung_database_is_bounded(monkeypatch, traces):
    monkeypatch.setattr(_nodes_mod, "_PERSIST_BUDGET_S", 0.05)
    real_sleep = asyncio.Event()

    async def _hang(*_a, **_k):
        await real_sleep.wait()  # never set

    pool = _pool(AsyncMock(side_effect=_hang))
    before = AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs")._value.get()
    started = time.monotonic()
    update = await asyncio.wait_for(persist_node(_state(pool)), timeout=5)
    assert time.monotonic() - started < 2
    assert update["response"].answer_run_id is None
    assert AGENTIC_PERSIST_FAILURES.labels(stage="answer_runs")._value.get() == before + 1
    assert len(traces) == 1  # the trace still goes out


@pytest.mark.asyncio
async def test_child_rows_go_out_in_one_batch():
    conn = MagicMock()
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield

    conn.transaction = _tx
    rows = [
        {"passage_id": str(uuid4()), "source_store": "qdrant",
         "candidate_ref": {"chunk_id": "a"}, "stage": "reranked"},
        {"passage_id": None, "source_store": "postgis",
         "candidate_ref": {"row": 1}},
    ]
    n = await _batched_retrieval_insert(
        conn, rows, {"a"}, "run", WS, "SQL_WITH", "SQL_NULL",
    )
    assert n == 2
    assert conn.executemany.await_count == 2
    conn.execute.assert_not_awaited()
    with_args = conn.executemany.await_args_list[0].args[1]
    assert with_args[0][-1] is True  # used_in_citation for chunk "a"


@pytest.mark.asyncio
async def test_rejected_batch_signals_per_row_fallback():
    conn = MagicMock()
    conn.executemany = AsyncMock(
        side_effect=asyncpg.exceptions.ForeignKeyViolationError("fk"),
    )
    assert await _batched_executemany(conn, "SQL", [(1,)]) is None
    assert await _batched_executemany(conn, "SQL", []) == 0


# ---------------------------------------------------------------------------
# AGT-17
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_failure_keeps_ui_stamps_and_trace(traces):
    pool = _pool(AsyncMock(side_effect=asyncpg.exceptions.UndefinedColumnError("x")))
    state = _state(
        pool,
        query="what are PLS-22-08's assays?",
        query_original="what are its assays?",
        resolution_trace=[{"kind": "pronoun", "original_phrase": "its",
                           "resolved_to": "PLS-22-08", "source_turn_index": 0,
                           "confidence": 0.85}],
        resolution_confidence=0.85,
    )
    update = await persist_node(state)
    response = update["response"]
    assert response.multi_turn_resolution["original_query"] == "what are its assays?"
    assert isinstance(response.guard_error_codes, list)
    assert len(traces) == 1
    assert traces[0].answer_run_id is None
    assert traces[0].user_query == "what are its assays?"


def test_row_counts_read_dataclass_results():
    chunk = DocumentChunk(
        chunk_id="c", text="t", source_document_id="d", document_title="T",
        section_number=None, section_title=None, section=None, page=1,
        document_type="NI43", report_id="r", relevance_score=0.5,
    )
    assert _result_row_count(
        DocumentSearchResult(chunks=[chunk, chunk], count=2, data_source="Q"),
    ) == 2
    assay = AssayDataResult(
        samples=[], count=2332, element="U3O8_pct_e", available_elements=[],
        min_value=None, max_value=None, mean_value=None, median_value=None,
        data_source="PostGIS",
    )
    assert _result_row_count(assay) == 2332
    assert _result_row_count(object()) == 0


@pytest.mark.asyncio
async def test_trace_source_counts_are_not_zero(traces):
    chunk = DocumentChunk(
        chunk_id="c", text="t", source_document_id="d", document_title="T",
        section_number=None, section_title=None, section=None, page=1,
        document_type="NI43", report_id="r", relevance_score=0.5,
    )
    pool = _pool(AsyncMock(return_value={"answer_run_id": uuid4()}))
    state = _state(pool, tool_results=[
        ("search_documents", DocumentSearchResult(
            chunks=[chunk] * 3, count=3, data_source="Q",
        )),
    ])
    await persist_node(state)
    assert traces[0].raw_results_per_source.qdrant_dense == 3
    assert traces[0].candidate_count_pre_rerank == 3


# ---------------------------------------------------------------------------
# AGT-1 / RAG-22 — what the INSERT writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_writes_original_query_and_citation_mode(traces):
    fetchrow = AsyncMock(return_value={"answer_run_id": uuid4()})
    pool = _pool(fetchrow)
    state = _state(
        pool,
        query="what are PLS-22-08's assays?",
        query_original="what are its assays?",
    )
    await persist_node(state)
    sql, *args = fetchrow.await_args.args
    assert sql == _ANSWER_RUN_INSERT_SQL
    assert args[2] == "what are its assays?"
    assert "citation_mode" in sql
    assert "'posthoc_span_resolution'" in sql
