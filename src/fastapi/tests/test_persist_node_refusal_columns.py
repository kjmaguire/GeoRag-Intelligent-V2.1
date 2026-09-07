"""persist_node writes ``rejection_reason`` + ``hallucination_guard_results``.

Both columns had been in the schema since 2026-04-22 / 2026-05-20 and
nothing wrote them, so the ``answer_quality_watch`` refusal-rate and
guard-fire signals, the Trust Inspector accepted/rejected split and the
refusal-rate runbook all read zero. Since 2026-09-07 ``persist_node``
classifies the §4b guards once before the ``silver.answer_runs`` INSERT
and writes:

* ``$17 rejection_reason`` — NULL for an accepted answer; for a refusal,
  the ``refusal_payload.reason_code`` when a terminal repair strategy
  stamped one, else ``insufficient_evidence`` for a citation-less run,
  with any further guard codes appended in parentheses.
* ``$18 hallucination_guard_results`` — the migration 2026_05_20_020000
  envelope ``{schema_version, guards, captured_at}``; ``guards`` is empty
  when the chain ran clean.

The same change made ``citation_lifecycle_state = 'rejected'`` reachable:
the assembler's ``no-tool-call`` placeholder citation used to count as a
citation, so every citation-less run was persisted as ``committed``.

The pool is mocked (pattern from ``test_persist_node_retry.py``); the
INSERT's positional bind parameters are inspected directly.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.agent.agentic_retrieval import nodes as nodes_mod
from app.agent.agentic_retrieval.nodes import persist_node
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.models.rag import Citation, GeoRAGResponse

TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"

# Positional bind parameters of the answer_runs INSERT (sql is args[0]).
_CITATION_STATE_ARG = 5
_REJECTION_REASON_ARG = 17
_GUARD_RESULTS_ARG = 18


class _DepsStub:
    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool
        self.project_id: str | None = None
        self.workspace_id: str | None = TEST_WORKSPACE_ID


def _real_citation() -> Citation:
    return Citation(
        citation_id="[DATA-1]",
        citation_type="DATA",
        source_chunk_id="refusal-columns-chunk",
        document_title="persist_node refusal columns unit test",
        relevance_score=0.9,
    )


def _sentinel_citation() -> Citation:
    """The placeholder assemble_response appends when nothing was retrieved."""
    return Citation(
        citation_id="[DATA-1]",
        citation_type="DATA",
        source_chunk_id="no-tool-call",
        document_title="No tool call",
        relevance_score=0.0,
    )


def _response(
    citations: list[Citation], text: str = "Hole 36-1085 cuts sandstone [DATA-1]."
) -> GeoRAGResponse:
    return GeoRAGResponse(
        text=text,
        citations=citations,
        confidence=0.9,
        sources_used=[c.source_chunk_id for c in citations],
    )


def _state(response: GeoRAGResponse, *, evidence: bool = True) -> AgenticRetrievalState:
    """``evidence=True`` gives the guard classifier a non-empty tool payload
    (otherwise it correctly reports NO_EVIDENCE_FOUND)."""
    return AgenticRetrievalState(
        query="tell me about hole 36-1085",
        deps=_DepsStub(pg_pool=None),
        intent="factual_lookup",
        effective_intent="factual_lookup",
        response=response,
        tool_results=(
            [("search_documents", [{"chunk_id": "refusal-columns-chunk"}])]
            if evidence
            else []
        ),
        run_start_monotonic=time.monotonic(),
    )


def _pool() -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"answer_run_id": uuid4()})
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 0")

    @asynccontextmanager
    async def _acquire() -> Any:
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._conn = conn
    return pool


def _answer_runs_args(pool: MagicMock) -> tuple[Any, ...]:
    for call in pool._conn.fetchrow.call_args_list:
        if "INSERT INTO silver.answer_runs" in call.args[0]:
            return call.args
    raise AssertionError("persist_node never issued the answer_runs INSERT")


@pytest.fixture(autouse=True)
def _quiet_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trace / child-row writers are best-effort; keep them out of the way."""
    monkeypatch.setattr(nodes_mod, "_insert_answer_run_with_retry", _direct_insert)


async def _direct_insert(pg_pool: Any, sql: str, *args: Any) -> Any:
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


@pytest.mark.asyncio
async def test_accepted_answer_writes_null_reason_and_clean_envelope() -> None:
    state = _state(_response([_real_citation()]))
    pool = _pool()
    state.deps.pg_pool = pool

    await persist_node(state)

    args = _answer_runs_args(pool)
    assert args[_CITATION_STATE_ARG] == "committed"
    assert args[_REJECTION_REASON_ARG] is None
    envelope = json.loads(args[_GUARD_RESULTS_ARG])
    assert envelope["schema_version"] == 1
    assert envelope["guards"] == {}
    assert envelope["captured_at"]


@pytest.mark.asyncio
async def test_sentinel_only_citations_persist_as_rejected_with_reason() -> None:
    state = _state(
        _response(
            [_sentinel_citation()], text="I don't have data on that in this project."
        ),
        evidence=False,
    )
    pool = _pool()
    state.deps.pg_pool = pool

    await persist_node(state)

    args = _answer_runs_args(pool)
    assert args[_CITATION_STATE_ARG] == "rejected"
    reason = args[_REJECTION_REASON_ARG]
    assert reason is not None
    assert reason.split(" ")[0] == "insufficient_evidence"
    envelope = json.loads(args[_GUARD_RESULTS_ARG])
    assert envelope["guards"]["NO_EVIDENCE_FOUND"] == {"status": "fail"}
    assert envelope["guards"]["CITATION_INCOMPLETE"] == {"status": "fail"}
    # The classifier's other findings ride along, so nothing is lost.
    assert (
        reason
        == "insufficient_evidence (guards: NO_EVIDENCE_FOUND, CITATION_INCOMPLETE)"
    )


@pytest.mark.asyncio
async def test_terminal_refusal_payload_supplies_the_reason_code() -> None:
    response = _response([_real_citation()])
    response.refusal_payload = {
        "type": "refusal",
        "reason_code": "MISSING_ASSAY_UNITS",
        "strategy": "REQUEST_UNIT_CLARIFICATION",
        "message": "Which assay units?",
        "candidates": [],
        "guard_codes": ["MISSING_ASSAY_UNITS"],
    }
    state = _state(response)
    pool = _pool()
    state.deps.pg_pool = pool

    await persist_node(state)

    args = _answer_runs_args(pool)
    # Citations survived, so the lifecycle state stays committed …
    assert args[_CITATION_STATE_ARG] == "committed"
    # … but the client renders a refusal_payload as rejected, and the
    # accepted/rejected split keys on rejection_reason.
    assert args[_REJECTION_REASON_ARG].startswith("MISSING_ASSAY_UNITS")


@pytest.mark.asyncio
async def test_guard_classifier_failure_still_persists_the_row(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(**_kwargs: Any) -> list[Any]:
        raise RuntimeError("classifier exploded")

    import app.agent.guards as guards_mod

    monkeypatch.setattr(guards_mod, "classify_guards", _boom)
    state = _state(_response([_real_citation()]))
    pool = _pool()
    state.deps.pg_pool = pool

    await persist_node(state)

    args = _answer_runs_args(pool)
    assert args[_REJECTION_REASON_ARG] is None
    assert json.loads(args[_GUARD_RESULTS_ARG])["guards"] == {}
    assert any("guard classification failed" in r.getMessage() for r in caplog.records)


def test_rejection_reason_helper_prefers_payload_then_insufficient_evidence() -> None:
    build = nodes_mod._build_rejection_reason

    accepted = _state(_response([_real_citation()]))
    assert build(accepted, "committed", []) is None
    assert build(accepted, "committed", ["CONFLICTING_SOURCES"]) is None

    rejected = _state(_response([_sentinel_citation()]))
    assert build(rejected, "rejected", []) == "insufficient_evidence"
    assert (
        build(rejected, "rejected", ["CITATION_INCOMPLETE", "NO_EVIDENCE_FOUND"])
        == "insufficient_evidence (guards: CITATION_INCOMPLETE, NO_EVIDENCE_FOUND)"
    )

    rejected.response.refusal_payload = {"reason_code": "AMBIGUOUS_HOLE_ID"}
    assert (
        build(rejected, "rejected", ["AMBIGUOUS_HOLE_ID", "CITATION_INCOMPLETE"])
        == "AMBIGUOUS_HOLE_ID (guards: CITATION_INCOMPLETE)"
    )


def test_guard_envelope_marks_conflict_as_notice() -> None:
    env = nodes_mod._build_guard_results(
        ["NUMERIC_GROUNDING_FAILED", "CONFLICTING_SOURCES"]
    )
    assert env["guards"] == {
        "NUMERIC_GROUNDING_FAILED": {"status": "fail"},
        "CONFLICTING_SOURCES": {"status": "notice"},
    }


def test_answer_quality_watch_reads_the_guards_object() -> None:
    # Read the source rather than import it: the module builds a Hatchet
    # client at import time, which needs HATCHET_CLIENT_TOKEN.
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "hatchet_workflows"
        / "answer_quality_watch.py"
    ).read_text(encoding="utf-8")
    window_sql = src[
        src.index("WINDOW_SQL = ") : src.index("class AnswerQualityWatchInput")
    ]

    assert "hallucination_guard_results -> 'guards'" in window_sql
    assert "rejection_reason IS NOT NULL" in window_sql
