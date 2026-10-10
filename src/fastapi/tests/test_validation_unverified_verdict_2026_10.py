"""Audit 2026-10 finding 3: "the checks did not run" is not "the checks passed".

When ``run_post_assembly_validation`` raises, ``validate_node`` fails closed
(confidence floored, banner prepended, ``validation_state="unverified"``) and
adds a Layer 3/4/6 warning. That warning matched no ``_WARNING_PATTERNS`` row, so
``persist_node`` wrote ``hallucination_guard_results = {"guards": {}}`` (the
column's own spelling of "the chain ran clean"), and the trace's GuardResults
booleans stayed at their default True.
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
from app.agent.agentic_retrieval.nodes import _build_guard_results, persist_node, validate_node
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.guards import (
    VALIDATION_RAISED_WARNING,
    VALIDATION_UNVERIFIED_KEY,
    GuardErrorCode,
    classify_guards,
    validation_did_not_complete,
)
from app.models.rag import Citation, GeoRAGResponse

WORKSPACE = "a0000000-0000-0000-0000-000000000001"
_GUARD_RESULTS_ARG = 18


def test_the_fail_closed_warning_text_is_pinned() -> None:
    """persist_node recognises the run by this text. Rewording it turns the
    detection off with no other signal, so the text is pinned here."""
    assert VALIDATION_RAISED_WARNING == (
        "Layer 3/4/6: post-assembly validation raised an exception before "
        "numeric grounding, entity resolution, and constraint checks could "
        "complete — this answer is UNVERIFIED, not confirmed clean."
    )


def test_it_is_not_a_guard_error_code_and_matches_no_pattern() -> None:
    """Deliberate: it is the absence of a verdict, not a numeric or entity
    failure, so it must not borrow either code (and its user-facing message)."""
    assert classify_guards(validation_warnings=[VALIDATION_RAISED_WARNING]) == []
    assert VALIDATION_UNVERIFIED_KEY not in {m.value for m in GuardErrorCode}


def test_detection_reads_the_warning_list() -> None:
    assert validation_did_not_complete([VALIDATION_RAISED_WARNING]) is True
    assert validation_did_not_complete(["Layer 3: Ungrounded number 2.31"]) is False
    assert validation_did_not_complete([]) is False
    assert validation_did_not_complete(None) is False


def test_the_envelope_records_an_incomplete_chain_as_a_failed_entry() -> None:
    envelope = _build_guard_results([], validation_incomplete=True)

    assert envelope["guards"] == {
        VALIDATION_UNVERIFIED_KEY: {
            "status": "fail",
            "reason": "post_assembly_validation_raised",
        }
    }


def test_the_envelope_for_a_clean_run_is_still_empty() -> None:
    assert _build_guard_results([])["guards"] == {}


def test_other_findings_ride_along_with_the_incomplete_marker() -> None:
    envelope = _build_guard_results(["CITATION_INCOMPLETE"], validation_incomplete=True)

    assert set(envelope["guards"]) == {"CITATION_INCOMPLETE", VALIDATION_UNVERIFIED_KEY}


# ---------------------------------------------------------------------------
# validate_node emits exactly that text
# ---------------------------------------------------------------------------
def _response() -> GeoRAGResponse:
    return GeoRAGResponse(
        text="Hole PLS-22-08 reached 510 m. [DATA-1]",
        citations=[
            Citation(
                citation_id="[DATA-1]", citation_type="DATA",
                source_chunk_id="silver.collars:count=1:first=abc",
                document_title="Collar data", relevance_score=0.9,
            )
        ],
        sources_used=["[DATA-1]"],
        confidence=0.8,
    )


class _Deps:
    pg_pool = None
    project_id = "00000000-0000-0000-0000-0000000000aa"
    workspace_id = WORKSPACE


@pytest.mark.asyncio
async def test_validate_node_stamps_the_pinned_text_when_validation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.hallucination.orchestrator_validators as validators

    async def boom(resp: Any, tool_results: Any, deps: Any) -> Any:
        raise RuntimeError("a guard blew up")

    monkeypatch.setattr(validators, "run_post_assembly_validation", boom)
    state = AgenticRetrievalState(query="q", deps=_Deps()).model_copy(
        update={"response": _response()}
    )

    update = await validate_node(state)

    assert VALIDATION_RAISED_WARNING in update["validation_warnings"]
    assert update["response"].validation_state == "unverified"


# ---------------------------------------------------------------------------
# persist_node writes the honest verdict
# ---------------------------------------------------------------------------
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


async def _direct_insert(pg_pool: Any, sql: str, *args: Any) -> Any:
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


def _persist_state(warnings: list[str]) -> AgenticRetrievalState:
    pool = _pool()
    deps = _Deps()
    deps.pg_pool = pool  # type: ignore[assignment]
    return AgenticRetrievalState(
        query="tell me about hole PLS-22-08", deps=deps, intent="factual_lookup",
        effective_intent="factual_lookup", response=_response(),
        tool_results=[("search_documents", [{"chunk_id": "x"}])],
        validation_warnings=warnings,
        run_start_monotonic=time.monotonic(),
    )


def _answer_runs_args(pool: MagicMock) -> tuple[Any, ...]:
    for call in pool._conn.fetchrow.call_args_list:
        if "INSERT INTO silver.answer_runs" in call.args[0]:
            return call.args
    raise AssertionError("persist_node never issued the answer_runs INSERT")


@pytest.fixture
def captured_trace(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    monkeypatch.setattr(nodes_mod, "_insert_answer_run_with_retry", _direct_insert)
    traces: list[Any] = []

    async def _enqueue(pool: Any, trace: Any) -> None:
        traces.append(trace)

    monkeypatch.setattr("app.services.trace_writer.enqueue_trace", _enqueue)
    return traces


@pytest.mark.asyncio
async def test_a_run_whose_validation_raised_is_not_persisted_as_clean(
    captured_trace: list[Any],
) -> None:
    state = _persist_state([VALIDATION_RAISED_WARNING])

    await persist_node(state)

    envelope = json.loads(_answer_runs_args(state.deps.pg_pool)[_GUARD_RESULTS_ARG])
    assert envelope["guards"] != {}
    assert envelope["guards"][VALIDATION_UNVERIFIED_KEY]["status"] == "fail"


@pytest.mark.asyncio
async def test_a_run_whose_validation_raised_does_not_pass_the_trace_guards(
    captured_trace: list[Any],
) -> None:
    state = _persist_state([VALIDATION_RAISED_WARNING])

    await persist_node(state)

    assert len(captured_trace) == 1
    guards = captured_trace[0].guard_results
    assert guards.numeric_grounding is False
    assert guards.entity_grounding is False


@pytest.mark.asyncio
async def test_a_run_that_was_validated_clean_is_still_persisted_clean(
    captured_trace: list[Any],
) -> None:
    state = _persist_state([])

    await persist_node(state)

    envelope = json.loads(_answer_runs_args(state.deps.pg_pool)[_GUARD_RESULTS_ARG])
    assert envelope["guards"] == {}
    guards = captured_trace[0].guard_results
    assert guards.numeric_grounding is True
    assert guards.entity_grounding is True
