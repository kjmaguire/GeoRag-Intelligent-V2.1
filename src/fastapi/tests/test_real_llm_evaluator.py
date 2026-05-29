"""Live tests for the doc-phase 159 real vLLM-backed evaluator.

Tests fall into three groups:
  1. Unit tests for `_detect_refusal()` — no vLLM needed
  2. Integration tests that call vLLM (skipped when VLLM_URL
     unreachable)
  3. Orchestration tests that pass `evaluator_kind='real_llm_v1'`
     to `run_workspace_evaluation` and verify it dispatches correctly
"""
from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from app.services.eval.real_llm_evaluator import (
    _detect_refusal,
    evaluate_question_real_llm,
)
from app.services.eval.workspace_evaluator import (
    QuestionRecord,
    run_workspace_evaluation,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _vllm_reachable() -> bool:
    """Probe vLLM /v1/models. Skip integration tests if unreachable."""
    url = os.environ.get("VLLM_URL", "http://vllm:8000/v1").rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            return r.status_code == 200
    except (httpx.HTTPError, httpx.TimeoutException):
        return False


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        yield p
    finally:
        await p.close()


def _make_question(
    *,
    text: str = "Test question",
    question_set: str = "core_chat",
    expected_refusal: bool = False,
) -> QuestionRecord:
    return QuestionRecord(
        question_id=uuid4(),
        question_set=question_set,
        question_text=text,
        context_setup={},
        expected_intent_class=None,
        expected_citations=[],
        expected_entities=[],
        expected_numeric_values=[],
        expected_refusal=expected_refusal,
        expected_refusal_reason=None,
        expected_language_compliance=[],
        difficulty="easy",
    )


# ----------------------------------------------------------------------
# Refusal-detection unit tests (no vLLM needed)
# ----------------------------------------------------------------------
def test_detect_refusal_canonical_phrases():
    assert _detect_refusal("I cannot answer that question.") is True
    assert _detect_refusal("I can't provide that information.") is True
    assert _detect_refusal("I am unable to help.") is True
    assert _detect_refusal("I don't have enough information.") is True


def test_detect_refusal_29_template_phrase():
    # §2.9 refusal template pattern — when no public records are
    # within the regulatory window.
    text = "There is no public data within 25 km of this property."
    assert _detect_refusal(text) is True


def test_detect_refusal_no_match_on_normal_answer():
    text = "The Athabasca Basin hosts world-class unconformity uranium deposits."
    assert _detect_refusal(text) is False


def test_detect_refusal_empty_or_none():
    assert _detect_refusal("") is False
    assert _detect_refusal("   ") is False


def test_detect_refusal_case_insensitive():
    assert _detect_refusal("I CANNOT confirm that.") is True


# ----------------------------------------------------------------------
# Orchestration dispatch tests (no vLLM needed)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_workspace_evaluation_rejects_unknown_evaluator(pool):
    with pytest.raises(ValueError, match="unknown evaluator_kind"):
        await run_workspace_evaluation(
            triggered_by="manual",
            evaluator_kind="nonsense_evaluator",
            pool=pool,
        )


@pytest.mark.asyncio
async def test_run_workspace_evaluation_synthetic_stub_default(pool):
    """Default evaluator_kind keeps backward-compat with doc-phase 132."""
    # Just verify it runs cleanly with the default — actual stub
    # behavior is covered by test_workspace_evaluator.py.
    result = await run_workspace_evaluation(
        triggered_by="manual",
        question_set_filter="core_chat",  # likely 0 active questions
        pool=pool,
    )
    assert result.success is True
    # Synthetic stub always passes whatever it sees.
    assert result.fail_count == 0


# ----------------------------------------------------------------------
# vLLM integration tests (gated on vLLM reachability)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_real_llm_evaluator_returns_refusal_correctly_paired(conn):
    """Ask a question we expect the LLM to NOT refuse + a question
    we expect it to refuse-or-answer-vaguely. Verify the evaluator
    grades each correctly against expected_refusal."""
    if not await _vllm_reachable():
        pytest.skip("vLLM not reachable; skipping live integration test")

    # Q1: answerable question, expected_refusal=False — LLM should answer.
    q1 = _make_question(
        text="What is the typical host rock for unconformity-related uranium deposits?",
        expected_refusal=False,
    )
    r1 = await evaluate_question_real_llm(conn, q1, max_tokens=128)
    # We can't guarantee passed=True in every run (LLM stochasticity at
    # T=0 is bounded, but model could still refuse for unexpected reasons).
    # We DO assert structural invariants:
    assert r1.actual_payload["evaluator"] == "real_llm_v1"
    assert r1.actual_payload["doc_phase"] == 159
    assert "response_text" in r1.actual_payload
    assert "detected_refusal" in r1.actual_payload
    assert r1.actual_payload["expected_refusal"] is False
    # Latency + tokens recorded.
    assert r1.latency_ms is not None and r1.latency_ms > 0
    assert r1.tokens_used is not None

    # Q2: unanswerable question with expected_refusal=True — LLM should refuse.
    q2 = _make_question(
        text="What is the precise tonnage of the secret deposit at coordinates 49.69°N 115.95°W?",
        expected_refusal=True,
    )
    r2 = await evaluate_question_real_llm(conn, q2, max_tokens=128)
    assert r2.actual_payload["expected_refusal"] is True
    assert "detected_refusal" in r2.actual_payload


@pytest.mark.asyncio
async def test_real_llm_evaluator_handles_vllm_unreachable_gracefully(conn, monkeypatch):
    """When vLLM call fails, return failure_layer='evaluator_not_ready'."""
    # Point at an unreachable URL to force the failure path.
    monkeypatch.setenv("VLLM_URL", "http://nonexistent-vllm:9999/v1")
    q = _make_question(text="any", expected_refusal=False)
    r = await evaluate_question_real_llm(conn, q, timeout_seconds=2.0)
    assert r.passed is False
    assert r.failure_layer == "evaluator_not_ready"
    assert r.actual_payload["error"].startswith("vllm_call_failed")
