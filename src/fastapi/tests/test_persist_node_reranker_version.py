"""persist_node writes ``answer_runs.reranker_version``.

The column has existed since 2026-04-21 and nothing wrote it, while
``app/services/reranker.py`` named it as the way a Rerank v4-scored run
and a 3.5-scored run stay distinguishable after the fact. Re-measuring
``RERANKER_SCORE_THRESHOLD_HOSTED`` from traffic
(``ops/validation/rerank_threshold_probe.py --harvest-since``) reads it.

It records what was USED: a run whose document searches all fell back to
RRF order is ``degraded:rrf``, never the configured model's version,
because its scores are fusion ranks and would poison a threshold fit.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.agentic_retrieval import nodes as nodes_mod
from app.agent.agentic_retrieval.nodes import persist_node
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.models.rag import Citation, GeoRAGResponse

TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"
_RERANKER_VERSION_ARG = 19  # sql is args[0]


class _DepsStub:
    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool
        self.project_id: str | None = None
        self.workspace_id: str | None = TEST_WORKSPACE_ID


def _chunk() -> DocumentChunk:
    return DocumentChunk(
        chunk_id="c-1",
        text="The collar sits at 1250 m.",
        source_document_id="doc-1",
        document_title="Test report",
        section_number=None,
        section_title=None,
        section=None,
        page=3,
        document_type="NI43",
        report_id="r-1",
        relevance_score=0.8,
    )


def _search(*, degraded: bool, chunks: bool = True) -> DocumentSearchResult:
    items = [_chunk()] if chunks else []
    return DocumentSearchResult(
        chunks=items, count=len(items), data_source="qdrant:georag_chunks", rerank_degraded=degraded,
    )


def _state(tool_results: list[tuple[str, Any]], pool: Any) -> AgenticRetrievalState:
    citation = Citation(
        citation_id="[NI43-1]",
        citation_type="NI43",
        source_chunk_id="c-1",
        document_title="Test report",
        relevance_score=0.8,
    )
    return AgenticRetrievalState(
        query="what elevation is the collar?",
        deps=_DepsStub(pg_pool=pool),
        intent="factual_lookup",
        effective_intent="factual_lookup",
        response=GeoRAGResponse(
            text="The collar sits at 1250 m [NI43-1].",
            citations=[citation],
            confidence=0.9,
            sources_used=["c-1"],
        ),
        tool_results=tool_results,
        run_start_monotonic=time.monotonic(),
    )


def _pool() -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"answer_run_id": "00000000-0000-0000-0000-00000000abcd"})
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 0")

    @asynccontextmanager
    async def _acquire() -> Any:
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._conn = conn
    return pool


async def _direct_insert(pg_pool: Any, sql: str, *args: Any) -> Any:
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


@pytest.fixture(autouse=True)
def _quiet_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nodes_mod, "_insert_answer_run_with_retry", _direct_insert)
    monkeypatch.setattr(
        "app.services.reranker.active_reranker_version", lambda: "cohere-bedrock:cohere.rerank-v3-5:0"
    )


async def _written_version(tool_results: list[tuple[str, Any]]) -> Any:
    pool = _pool()
    await persist_node(_state(tool_results, pool))
    for call in pool._conn.fetchrow.call_args_list:
        if "INSERT INTO silver.answer_runs" in call.args[0]:
            assert "reranker_version" in call.args[0]
            return call.args[_RERANKER_VERSION_ARG]
    raise AssertionError("persist_node never issued the answer_runs INSERT")


@pytest.mark.asyncio
async def test_a_reranked_run_records_the_model_that_scored_it() -> None:
    version = await _written_version([("search_documents", _search(degraded=False))])
    assert version == "cohere-bedrock:cohere.rerank-v3-5:0"


@pytest.mark.asyncio
async def test_an_all_fallback_run_is_marked_degraded_not_the_configured_model() -> None:
    """Today's production state (Rerank denied by IAM): every query is RRF-ordered."""
    version = await _written_version([("search_documents", _search(degraded=True))])
    assert version == "degraded:rrf"


@pytest.mark.asyncio
async def test_one_reranked_search_is_enough_to_record_the_model() -> None:
    version = await _written_version(
        [("search_documents", _search(degraded=True)), ("search_documents", _search(degraded=False))]
    )
    assert version == "cohere-bedrock:cohere.rerank-v3-5:0"


@pytest.mark.asyncio
async def test_no_document_search_writes_null() -> None:
    assert await _written_version([("query_collars", [{"hole_id": "DH-1"}])]) is None


@pytest.mark.asyncio
async def test_an_empty_search_does_not_count_as_reranked() -> None:
    assert await _written_version([("search_documents", _search(degraded=False, chunks=False))]) is None


def test_the_value_fits_the_column() -> None:
    assert len(nodes_mod._RERANK_DEGRADED_VERSION) <= 64


# ---------------------------------------------------------------------------
# answer_runs.embedding_model (ADR-0025) -- the sibling column, same row
# ---------------------------------------------------------------------------
# The query path writes the model that embedded the question, so an answer can
# be traced to the vector space that retrieved it. With the Embed v4 -> Embed 5
# re-embed in flight that is the only per-row way to tell which space a refusal
# or a citation came from.

_EMBEDDING_MODEL_ARG = 20  # sql is args[0]; reranker_version is 19


class _NamedModel:
    """The surface the persist step reads off ``deps.embedding_model``."""

    model_name = "embed-v5.0-pro"
    query_model_name = "embed-v5.0-fast"


async def _written_embedding_model(tool_results: list[tuple[str, Any]], model: Any) -> Any:
    pool = _pool()
    state = _state(tool_results, pool)
    state.deps.embedding_model = model
    await persist_node(state)
    for call in pool._conn.fetchrow.call_args_list:
        if "INSERT INTO silver.answer_runs" in call.args[0]:
            assert "embedding_model," in call.args[0]
            return call.args[_EMBEDDING_MODEL_ARG]
    raise AssertionError("persist_node never issued the answer_runs INSERT")


@pytest.mark.asyncio
async def test_a_document_search_run_records_the_query_model() -> None:
    written = await _written_embedding_model(
        [("search_documents", _search(degraded=False))], _NamedModel()
    )
    assert written == "embed-v5.0-fast"


@pytest.mark.asyncio
async def test_a_run_with_no_document_search_records_no_embedding_model() -> None:
    assert await _written_embedding_model([], _NamedModel()) is None


@pytest.mark.asyncio
async def test_a_failed_search_records_no_embedding_model() -> None:
    failed = DocumentSearchResult(
        chunks=[], count=0, data_source="Qdrant (timeout)", retrieval_failure="timeout",
    )
    assert await _written_embedding_model([("search_documents", failed)], _NamedModel()) is None


# ---------------------------------------------------------------------------
# Audit 2026-10-04 item 10: RRF scores never masquerade as reranker scores
# ---------------------------------------------------------------------------


def _doc_result(*, degraded: bool, score: float):
    from app.agent.tools import DocumentChunk, DocumentSearchResult

    chunk = DocumentChunk(
        chunk_id="b5c3f0e2-9d8a-4a55-8d66-0a1b2c3d4e5f", text="Resource 12.5 Mt",
        source_document_id="rep-1", document_title="R", section_number=None,
        section_title=None, section=None, page=1, document_type="NI43",
        report_id="rep-1", relevance_score=score,
    )
    return DocumentSearchResult(
        chunks=[chunk], count=1, data_source="qdrant", rerank_degraded=degraded,
    )


def test_a_reranked_chunk_keeps_its_reranker_score() -> None:
    from app.agent.agentic_retrieval.nodes import _extract_retrieval_rows

    (row,) = _extract_retrieval_rows([("search_documents", _doc_result(degraded=False, score=0.83))])
    assert row["stage"] == "reranked"
    assert row["reranker_score"] == 0.83
    assert row["retriever_score"] is None


def test_a_degraded_chunk_is_not_a_reranked_row() -> None:
    from app.agent.agentic_retrieval.nodes import _extract_retrieval_rows

    (row,) = _extract_retrieval_rows([("search_documents", _doc_result(degraded=True, score=0.0328))])
    # 'retrieved' is the stage the table's CHECK allows; the harvest reads
    # only stage='reranked'.
    assert row["stage"] == "retrieved"
    assert row["retriever_score"] == 0.0328
    assert row["reranker_score"] is None


def test_the_stage_is_one_the_table_accepts() -> None:
    import pathlib
    import re

    from app.agent.agentic_retrieval.nodes import _extract_retrieval_rows

    migrations = pathlib.Path(__file__).resolve().parents[3] / "database" / "migrations"
    if not migrations.is_dir():
        pytest.skip("migrations directory not available in this checkout")
    ddl = (migrations / "2026_04_21_110000_create_answer_retrieval_items.php").read_text()
    allowed = set(re.findall(r"CHECK \(stage IN \(([^)]*)\)", ddl)[0].replace("\\'", "").replace(" ", "").split(","))
    for degraded in (False, True):
        (row,) = _extract_retrieval_rows(
            [("search_documents", _doc_result(degraded=degraded, score=0.5))]
        )
        assert row["stage"] in allowed
