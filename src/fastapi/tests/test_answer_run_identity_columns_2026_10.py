"""Audit 2026-10 finding 24: silver.answer_runs records who asked, and against what.

``persist_node``'s INSERT hard-coded ``workspace_data_version_at_query`` to the
literal ``0``, never wrote ``project_data_version_at_query``, ``user_id`` or
``trace_id``, so a row could be joined neither to the person who asked nor to
the request's log lines, and the staleness comparison (recorded data version vs
the workspace's current one) could never fire.

``session_id`` is deliberately NOT covered: Laravel's ``StreamQueryFromFastApi``
posts ``query_id, project_id, query, context_envelope, history`` and no
conversation id, so there is nothing on this side to record (reported as
needs-backend).

The pool is mocked (pattern from ``test_persist_node_refusal_columns.py``); the
statement is inspected as text and its positional binds directly. The SQL itself
was also run against a real PostgreSQL 16 schema when this was written.
"""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.agent.agentic_retrieval import nodes as nodes_mod
from app.agent.agentic_retrieval.nodes import (
    _ANSWER_RUN_INSERT_SQL,
    _answer_run_trace_id,
    _answer_run_user_id,
    persist_node,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.models.rag import Citation, GeoRAGResponse

WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"

# Positional bind parameters of the answer_runs INSERT (sql is args[0]).
_EMBEDDING_MODEL_ARG = 20
_USER_ID_ARG = 21
_TRACE_ID_ARG = 22


# ---------------------------------------------------------------------------
# The statement
# ---------------------------------------------------------------------------


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _columns_to_values() -> dict[str, str]:
    """Pair each INSERT column with the expression that fills it."""
    sql = re.sub(r"--[^\n]*", "", _ANSWER_RUN_INSERT_SQL)
    head, _, rest = sql.partition("INSERT INTO silver.answer_runs (")
    columns_text, _, rest = rest.partition(") VALUES (")
    values_text, _, _ = rest.rpartition(")\n    RETURNING")
    columns = [c.strip() for c in columns_text.split(",") if c.strip()]
    values = _split_top_level(values_text)
    assert len(columns) == len(values), (
        f"{len(columns)} columns but {len(values)} values: the INSERT has drifted"
    )
    return dict(zip(columns, values, strict=True))


def _compact(expression: str) -> str:
    return re.sub(r"\s+", " ", expression).strip()


def test_every_column_has_exactly_one_value() -> None:
    pairs = _columns_to_values()
    assert len(pairs) == 25


def test_the_workspace_data_version_is_read_not_hard_coded() -> None:
    value = _compact(_columns_to_values()["workspace_data_version_at_query"])

    assert value != "0"
    assert "silver.workspaces w WHERE w.workspace_id = $1::uuid" in value
    assert "SELECT w.data_version" in value
    # NOT NULL column: a row that is not visible must not null the INSERT.
    assert value.startswith("COALESCE(") and value.endswith(", 0 )")


def test_the_project_data_version_is_read_and_null_when_the_project_is_unknown() -> None:
    value = _compact(_columns_to_values()["project_data_version_at_query"])

    assert value == "(SELECT p.data_version FROM silver.projects p WHERE p.project_id = $2::uuid)"
    # Same lookup shape as project_id, so the two agree about an unknown project.
    project = _compact(_columns_to_values()["project_id"])
    assert project == "(SELECT p.project_id FROM silver.projects p WHERE p.project_id = $2::uuid)"


def test_the_user_is_resolved_in_a_subselect_so_an_unknown_id_cannot_cost_the_row() -> None:
    # answer_runs.user_id REFERENCES public.users ON DELETE RESTRICT: binding the
    # id directly would turn an id that is not in public.users into a foreign-key
    # violation, which is not transient and loses the run's whole row.
    value = _compact(_columns_to_values()["user_id"])

    assert value == "(SELECT u.id FROM public.users u WHERE u.id = $21::bigint)"


def test_the_trace_id_and_embedding_model_binds_are_where_the_callers_expect() -> None:
    pairs = _columns_to_values()

    assert _compact(pairs["embedding_model"]) == "$20"
    assert _compact(pairs["trace_id"]) == "$22"
    assert "$23" not in _ANSWER_RUN_INSERT_SQL


# ---------------------------------------------------------------------------
# The binds
# ---------------------------------------------------------------------------


def test_a_jwt_sub_claim_becomes_the_user_id() -> None:
    assert _answer_run_user_id(SimpleNamespace(user_id="42")) == 42
    assert _answer_run_user_id(SimpleNamespace(user_id=" 7 ")) == 7


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "service",
        "eval-harness",
        "-3",
        "+3",
        "0",
        "4.2",
        "1e3",
        "٣",  # an Arabic-Indic digit: str.isdigit() is True, int() would accept it
        "²",  # superscript two: isdigit() is True, int() raises
        str(2**63),  # one past BIGINT
        "9" * 40,
    ],
)
def test_a_sub_claim_that_cannot_be_a_user_id_records_null(raw: object) -> None:
    assert _answer_run_user_id(SimpleNamespace(user_id=raw)) is None


def test_the_largest_bigint_is_accepted() -> None:
    assert _answer_run_user_id(SimpleNamespace(user_id=str(2**63 - 1))) == 2**63 - 1


def test_deps_without_the_attributes_record_null() -> None:
    assert _answer_run_user_id(SimpleNamespace()) is None
    assert _answer_run_trace_id(SimpleNamespace()) is None


def test_the_trace_id_is_trimmed_and_fits_the_column() -> None:
    assert _answer_run_trace_id(SimpleNamespace(trace_id="0af7651916cd43dd8448eb211c80319c")) == (
        "0af7651916cd43dd8448eb211c80319c"
    )
    assert _answer_run_trace_id(SimpleNamespace(trace_id="  abc  ")) == "abc"
    assert len(_answer_run_trace_id(SimpleNamespace(trace_id="x" * 200)) or "") == 64


@pytest.mark.parametrize("raw", [None, "", "   ", 12345, MagicMock()])
def test_a_missing_or_non_string_trace_id_records_null(raw: object) -> None:
    assert _answer_run_trace_id(SimpleNamespace(trace_id=raw)) is None


# ---------------------------------------------------------------------------
# persist_node end to end (mocked pool)
# ---------------------------------------------------------------------------


class _Deps:
    def __init__(self, pool: Any, *, user_id: Any, trace_id: Any) -> None:
        self.pg_pool = pool
        self.project_id: str | None = None
        self.workspace_id: str | None = WORKSPACE_ID
        self.user_id = user_id
        self.trace_id = trace_id


def _pool() -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"answer_run_id": uuid4(), "project_id": None})
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 0")

    @asynccontextmanager
    async def _acquire() -> Any:
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._conn = conn
    return pool


def _state(pool: Any, *, user_id: Any, trace_id: Any) -> AgenticRetrievalState:
    citation = Citation(
        citation_id="[DATA-1]",
        citation_type="DATA",
        source_chunk_id="identity-columns-chunk",
        document_title="identity columns unit test",
        relevance_score=0.9,
    )
    response = GeoRAGResponse(
        text="Hole 36-1085 cuts sandstone [DATA-1].",
        citations=[citation],
        confidence=0.9,
        sources_used=[citation.source_chunk_id],
    )
    return AgenticRetrievalState(
        query="tell me about hole 36-1085",
        deps=_Deps(pool, user_id=user_id, trace_id=trace_id),
        intent="factual_lookup",
        effective_intent="factual_lookup",
        response=response,
        tool_results=[("search_documents", [{"chunk_id": "identity-columns-chunk"}])],
        run_start_monotonic=time.monotonic(),
    )


async def _direct_insert(pg_pool: Any, sql: str, *args: Any) -> Any:
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


@pytest.fixture(autouse=True)
def _direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nodes_mod, "_insert_answer_run_with_retry", _direct_insert)


def _insert_args(pool: MagicMock) -> tuple[Any, ...]:
    for call in pool._conn.fetchrow.call_args_list:
        if "INSERT INTO silver.answer_runs" in call.args[0]:
            return call.args
    raise AssertionError("persist_node never issued the answer_runs INSERT")


@pytest.mark.asyncio
async def test_persist_binds_the_asker_and_the_request_trace() -> None:
    pool = _pool()
    trace = "0af7651916cd43dd8448eb211c80319c"

    await persist_node(_state(pool, user_id="42", trace_id=trace))

    args = _insert_args(pool)
    assert args[0] == _ANSWER_RUN_INSERT_SQL
    assert args[_USER_ID_ARG] == 42
    assert args[_TRACE_ID_ARG] == trace
    assert len(args) == 1 + 22  # the statement and its twenty-two binds


@pytest.mark.asyncio
async def test_persist_keeps_the_original_binds_where_they_were() -> None:
    pool = _pool()

    await persist_node(_state(pool, user_id="42", trace_id="abc"))

    args = _insert_args(pool)
    assert args[1] == WORKSPACE_ID
    assert args[3].startswith("tell me about hole")
    assert args[5] == "committed"
    assert args[_EMBEDDING_MODEL_ARG] is None  # no document search ran in this state


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", [None, "service-account", "-1", str(2**70)])
async def test_a_run_without_a_usable_user_still_persists_with_null(sub: object) -> None:
    pool = _pool()

    await persist_node(_state(pool, user_id=sub, trace_id=None))

    args = _insert_args(pool)
    assert args[_USER_ID_ARG] is None
    assert args[_TRACE_ID_ARG] is None
