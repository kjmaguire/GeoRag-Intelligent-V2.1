"""Audit 2026-10-04: persist_node latency (item 17) and main.py (item 30)."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import (
    _ANSWER_RUN_INSERT_SQL,
    drain_persist_background,
    persist_node,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.models.rag import Citation, GeoRAGResponse

WS = "a0000000-0000-0000-0000-000000000001"
PROJECT = "019d74a1-fba8-7165-9ae6-a5bf93eef97d"


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool
        self.project_id = PROJECT
        self.workspace_id = WS


def _response() -> GeoRAGResponse:
    return GeoRAGResponse(
        text="Hole 36-1085 cuts the sandstone [DATA-1].",
        citations=[Citation(
            citation_id="[DATA-1]", citation_type="DATA",
            source_chunk_id="chunk-1", document_title="T", relevance_score=0.9,
        )],
        confidence=0.9, sources_used=["chunk-1"],
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


def _state(pool: Any) -> AgenticRetrievalState:
    return AgenticRetrievalState(
        query="tell me about hole 36-1085", deps=_Deps(pool), intent="factual_lookup",
        effective_intent="factual_lookup", response=_response(),
        run_start_monotonic=time.monotonic(),
    )


@pytest.fixture(autouse=True)
def _traces(monkeypatch):
    async def _enqueue(_pool, trace):
        return None

    import app.services.trace_writer as _tw

    monkeypatch.setattr(_tw, "enqueue_trace", _enqueue)


# ---------------------------------------------------------------------------
# Item 17 -- no FK pre-check round-trip
# ---------------------------------------------------------------------------


def test_insert_resolves_the_project_in_a_subselect() -> None:
    assert "SELECT p.project_id FROM silver.projects p WHERE p.project_id = $2::uuid" in (
        _ANSWER_RUN_INSERT_SQL
    )
    assert "RETURNING answer_run_id, project_id" in _ANSWER_RUN_INSERT_SQL
    # Placeholder count is unchanged: still 21 binds, $2 still the project id.
    assert "$20" in _ANSWER_RUN_INSERT_SQL and "$21" not in _ANSWER_RUN_INSERT_SQL


@pytest.mark.asyncio
async def test_persist_makes_no_separate_fk_lookup() -> None:
    run_id = uuid4()
    fetchrow = AsyncMock(return_value={"answer_run_id": run_id, "project_id": PROJECT})
    pool = _pool(fetchrow)
    update = await persist_node(_state(pool))
    await drain_persist_background()

    assert fetchrow.await_count == 1
    pool.conn.fetchval.assert_not_awaited()
    assert update["response"].answer_run_id == run_id
    # The project id the INSERT was handed is the caller's, untouched.
    assert fetchrow.await_args.args[2] == PROJECT


@pytest.mark.asyncio
async def test_unresolved_project_is_logged_not_fatal(caplog) -> None:
    import logging

    fetchrow = AsyncMock(return_value={"answer_run_id": uuid4(), "project_id": None})
    with caplog.at_level(logging.WARNING):
        update = await persist_node(_state(_pool(fetchrow)))
        await drain_persist_background()
    assert update["response"].answer_run_id is not None
    assert any("not present in silver.projects" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Item 17 -- child rows and usage metering are off the path to `completed`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_child_rows_do_not_delay_the_return(monkeypatch) -> None:
    release = asyncio.Event()
    finished: list[str] = []

    async def slow_children(**_kw: Any) -> tuple[int, int]:
        await release.wait()
        finished.append("children")
        return 1, 1

    async def usage(*_a: Any, **_k: Any) -> None:
        finished.append("usage")

    monkeypatch.setattr(_nodes_mod, "_persist_retrieval_and_citation_items", slow_children)
    monkeypatch.setattr(_nodes_mod, "_write_chat_usage_event", usage)

    fetchrow = AsyncMock(return_value={"answer_run_id": uuid4(), "project_id": PROJECT})
    update = await asyncio.wait_for(persist_node(_state(_pool(fetchrow))), timeout=2)

    # Returned (and stamped the id) while the children were still blocked.
    assert update["response"].answer_run_id is not None
    assert finished == []
    assert len(_nodes_mod._PERSIST_BACKGROUND_TASKS) == 1

    release.set()
    await drain_persist_background()
    assert finished == ["children", "usage"]
    assert not _nodes_mod._PERSIST_BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_background_children_keep_a_hard_upper_bound(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setattr(_nodes_mod, "_CHILD_ROWS_BUDGET_S", 0.05)
    never = asyncio.Event()
    usage_calls: list[int] = []

    async def hung_children(**_kw: Any) -> tuple[int, int]:
        await never.wait()
        return 0, 0

    async def usage(*_a: Any, **_k: Any) -> None:
        usage_calls.append(1)

    monkeypatch.setattr(_nodes_mod, "_persist_retrieval_and_citation_items", hung_children)
    monkeypatch.setattr(_nodes_mod, "_write_chat_usage_event", usage)
    fetchrow = AsyncMock(return_value={"answer_run_id": uuid4(), "project_id": PROJECT})
    with caplog.at_level(logging.ERROR):
        await persist_node(_state(_pool(fetchrow)))
        await asyncio.wait_for(drain_persist_background(), timeout=5)
    assert any("child-row INSERTs failed or exceeded their budget" in r.message
               for r in caplog.records)
    assert usage_calls == [1]  # metering still ran after the children timed out


@pytest.mark.asyncio
async def test_usage_failure_is_logged_not_lost_silently(monkeypatch, caplog) -> None:
    import logging

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("usage table down")

    async def children(**_kw: Any) -> tuple[int, int]:
        return 0, 0

    monkeypatch.setattr(_nodes_mod, "_write_chat_usage_event", boom)
    monkeypatch.setattr(_nodes_mod, "_persist_retrieval_and_citation_items", children)
    fetchrow = AsyncMock(return_value={"answer_run_id": uuid4(), "project_id": PROJECT})
    with caplog.at_level(logging.ERROR):
        await persist_node(_state(_pool(fetchrow)))
        await drain_persist_background()
    assert any("usage metering failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_usage_is_metered_even_when_the_insert_failed(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    async def usage(_pool: Any, **kw: Any) -> None:
        calls.append(kw)

    monkeypatch.setattr(_nodes_mod, "_write_chat_usage_event", usage)
    fetchrow = AsyncMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr(_nodes_mod, "_insert_answer_run_with_retry",
                        AsyncMock(side_effect=RuntimeError("down")))
    await persist_node(_state(_pool(fetchrow)))
    await drain_persist_background()
    assert len(calls) == 1
    assert calls[0]["answer_run_id"] is None


@pytest.mark.asyncio
async def test_drain_with_nothing_pending_returns_at_once() -> None:
    await asyncio.wait_for(drain_persist_background(timeout=0.1), timeout=1)


# ---------------------------------------------------------------------------
# Item 30 -- statement cache size is env-driven
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    (None, 0), ("", 0), ("  ", 0), ("0", 0), ("100", 100), (" 50 ", 50),
    ("abc", 0), ("-5", 0), ("1.5", 0),
])
def test_statement_cache_size_env(monkeypatch, raw: str | None, expected: int) -> None:
    from app.main import _statement_cache_size_from_env

    if raw is None:
        monkeypatch.delenv("ASYNCPG_STATEMENT_CACHE_SIZE", raising=False)
    else:
        monkeypatch.setenv("ASYNCPG_STATEMENT_CACHE_SIZE", raw)
    assert _statement_cache_size_from_env() == expected


def test_the_pool_is_built_from_the_env_value() -> None:
    import pathlib

    src = (pathlib.Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "statement_cache_size=_stmt_cache_size" in src
    assert "statement_cache_size=0," not in src
