"""Audit 2026-10 finding 2: every structured tool signals its own failure.

Four result types (downhole logs, project summary, coverage gap, public
geoscience) had no ``retrieval_failure`` field, so their timeout / error
branches returned an empty result that ``execute_node._note_retrieval_failure``
could not see. A Postgres outage then read as "no logs / no data / no
records". The spatial, overview, assay and collar-details results already
carried the field (see ``test_query_path_tools_audit_2026_10.py``).
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import execute_node
from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.deps import AgentDeps
from app.agent.public_geoscience_tool import (
    PublicGeoscienceSearchResult,
    search_public_geoscience,
)
from app.agent.tool_result_helpers import _is_empty_tool_result
from app.agent.tools import (
    CoverageGapResult,
    DocumentChunk,
    DocumentSearchResult,
    DownholeLogsResult,
    IngestGapStats,
    ProjectSummaryResult,
    query_coverage_gap,
    query_downhole_logs,
    query_project_summary,
)
from app.config import settings

PROJECT = "00000000-0000-0000-0000-0000000000aa"
WORKSPACE = "a0000000-0000-0000-0000-000000000001"


class _Txn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _BoomConn:
    """Every query raises ``exc``; ``transaction()`` and ``execute()`` work."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.transaction = MagicMock(return_value=_Txn())

    async def execute(self, *a: Any, **k: Any) -> None:
        return None

    async def fetch(self, *a: Any, **k: Any) -> Any:
        raise self._exc

    async def fetchrow(self, *a: Any, **k: Any) -> Any:
        raise self._exc


class _HangConn(_BoomConn):
    """Every query outlives the tool's PostGIS deadline."""

    async def fetch(self, *a: Any, **k: Any) -> Any:
        await asyncio.sleep(5)

    async def fetchrow(self, *a: Any, **k: Any) -> Any:
        await asyncio.sleep(5)


def _deps(conn: Any | None) -> AgentDeps:
    pool = None
    if conn is not None:
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return AgentDeps(
        pg_pool=pool, qdrant_client=None, neo4j_driver=None,  # type: ignore[arg-type]
        project_id=PROJECT, embedding_model=None, reranker=None,
        workspace_id=WORKSPACE,
    )


@dataclass
class _Ctx:
    deps: AgentDeps


async def _run_all(conn: Any | None) -> dict[str, Any]:
    deps = _deps(conn)
    return {
        "downhole": await query_downhole_logs(_Ctx(deps), project_id=PROJECT, hole_id="PLS-22-08"),
        "summary": await query_project_summary(deps, WORKSPACE, PROJECT),
        "coverage": await query_coverage_gap(deps, WORKSPACE, PROJECT),
        "public_geo": await search_public_geoscience(_Ctx(deps), text_query="athabasca"),
    }


# ---------------------------------------------------------------------------
# The tools say so
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [(RuntimeError("connection reset"), "error"), (TimeoutError(), "timeout")],
)
async def test_each_structured_tool_reports_a_failed_query(exc: Exception, expected: str) -> None:
    results = await _run_all(_BoomConn(exc))

    for name, result in results.items():
        assert result.retrieval_failure == expected, name
    assert isinstance(results["downhole"], DownholeLogsResult) and results["downhole"].count == 0
    assert isinstance(results["summary"], ProjectSummaryResult) and results["summary"].count == 0
    assert isinstance(results["coverage"], CoverageGapResult) and results["coverage"].count == 0
    assert isinstance(results["public_geo"], PublicGeoscienceSearchResult)
    assert results["public_geo"].count == 0


@pytest.mark.asyncio
async def test_a_query_that_outlives_the_postgis_deadline_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "TIMEOUT_POSTGIS_S", 0.01)

    deps = _deps(_HangConn(RuntimeError("unused")))

    assert (await query_downhole_logs(_Ctx(deps), PROJECT, "PLS-22-08")).retrieval_failure == "timeout"
    assert (await query_project_summary(deps, WORKSPACE, PROJECT)).retrieval_failure == "timeout"
    assert (await query_coverage_gap(deps, WORKSPACE, PROJECT)).retrieval_failure == "timeout"


@pytest.mark.asyncio
async def test_no_pool_is_a_failure_not_an_empty_project() -> None:
    deps = _deps(None)

    assert (await query_project_summary(deps, WORKSPACE, PROJECT)).retrieval_failure == "error"
    assert (await query_coverage_gap(deps, WORKSPACE, PROJECT)).retrieval_failure == "error"
    assert (await search_public_geoscience(_Ctx(deps), text_query="x")).retrieval_failure == "error"


@pytest.mark.asyncio
async def test_a_query_that_ran_and_found_nothing_is_not_a_failure() -> None:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=_Txn())
    deps = _deps(conn)

    assert (await query_downhole_logs(_Ctx(deps), PROJECT, "PLS-22-08")).retrieval_failure is None
    assert (await query_project_summary(deps, WORKSPACE, PROJECT)).retrieval_failure is None
    assert (await search_public_geoscience(_Ctx(deps), text_query="x")).retrieval_failure is None


@pytest.mark.asyncio
async def test_a_bad_request_is_the_callers_error_not_a_backend_failure() -> None:
    result = await search_public_geoscience(_Ctx(_deps(None)), bbox=[10.0, 10.0, 5.0, 5.0])

    assert result.error is not None
    assert result.retrieval_failure is None


# ---------------------------------------------------------------------------
# An empty summary / coverage result is empty (so an outage cannot be cited)
# ---------------------------------------------------------------------------
def _coverage(count: int) -> CoverageGapResult:
    return CoverageGapResult(
        ingest_gap=IngestGapStats(indexed=0, processed=0, gap_pct=0.0),
        attribute_coverage=[], findings=[], project_id=PROJECT,
        workspace_id=WORKSPACE, count=count,
    )


def _summary(count: int) -> ProjectSummaryResult:
    return ProjectSummaryResult(
        technique_breakdown=[], extraction_pending_fields=[], project_id=PROJECT,
        workspace_id=WORKSPACE, count=count,
    )


def test_zero_row_summary_and_coverage_results_are_empty() -> None:
    assert _is_empty_tool_result(_summary(0)) is True
    assert _is_empty_tool_result(_coverage(0)) is True


def test_populated_summary_and_coverage_results_are_not_empty() -> None:
    assert _is_empty_tool_result(_summary(3)) is False
    assert _is_empty_tool_result(_coverage(5)) is False


# ---------------------------------------------------------------------------
# execute_node sees the failure before the empty result is dropped
# ---------------------------------------------------------------------------
class _NodeDeps:
    project_id = PROJECT
    workspace_id = WORKSPACE
    pg_pool = None
    redis_client = None


def _doc_result() -> DocumentSearchResult:
    chunk = DocumentChunk(
        chunk_id="c1", text="Resource 12.5 Mt", source_document_id="rep-1",
        document_title="R", section_number=None, section_title=None, section=None,
        page=1, document_type="NI43", report_id="rep-1", relevance_score=0.9,
    )
    return DocumentSearchResult(chunks=[chunk], count=1, data_source="qdrant (reranked)")


FAILED = [
    ("query_downhole_logs", DownholeLogsResult(
        collar=None, intervals=[], count=0, data_source="PostGIS silver.lithology_logs",
        retrieval_failure="timeout"), "Downhole logs"),
    ("query_project_summary", dataclasses.replace(_summary(0), retrieval_failure="error"),
     "Project summary"),
    ("query_coverage_gap", dataclasses.replace(_coverage(0), retrieval_failure="timeout"),
     "Coverage data"),
    ("search_public_geoscience", PublicGeoscienceSearchResult(
        records=[], count=0, jurisdictions_queried=[], canonical_types_queried=[],
        retrieval_failure="error"), "Public geoscience records"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "failed", "label"), FAILED, ids=[f[0] for f in FAILED])
async def test_execute_node_surfaces_the_failure_and_drops_the_empty_result(
    monkeypatch: pytest.MonkeyPatch, tool: str, failed: Any, label: str
) -> None:
    async def fake_call(tool_name: str, query: str, deps: Any) -> Any:
        if tool_name == "search_documents":
            return _doc_result()
        return failed if tool_name == tool else None

    monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)
    profile = profile_for_intent("factual_lookup").model_copy(
        update={"primary_tools": ["search_documents", tool], "secondary_tools": []}
    )
    state = AgenticRetrievalState(
        query="what is the resource?", deps=_NodeDeps(), intent="factual_lookup",
        effective_intent="factual_lookup", retrieval_profile=profile,
    )

    update = await execute_node(state)

    assert update["retrieval_failures"] == [f"{label} (temporarily unavailable)"]
    assert [name for name, _ in update["tool_results"]] == ["search_documents"]
