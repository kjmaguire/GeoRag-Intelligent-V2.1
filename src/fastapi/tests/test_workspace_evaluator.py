"""Live tests for the §10.4 workspace evaluator (doc-phase 132).

Verifies the live orchestration:
  - run_summaries row inserted at start, updated with finals
  - run_results row per question with synthetic_stub payload
  - regression_count computed correctly against prior runs
  - audit anchor emitted on completion
  - promotion gate logic (warning_only vs blocking modes)

The per-question evaluator is a synthetic stub; these tests exercise
the orchestration and the gate, not the real RAG evaluator.

Requires a real Postgres connection (eval.golden_questions +
eval.run_summaries + eval.run_results tables) and at least one
active golden question row from the doc-phase 124 mechanical seed.
"""
from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.services.eval.thresholds import RegressionThresholds, check_promotion_gate
from app.services.eval.workspace_evaluator import (
    QuestionRecord,
    QuestionResult,
    evaluate_question,
    run_workspace_evaluation,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_user(conn):
    """Insert + tear down a user row for the test."""
    email = f"test-evalrunner-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        """
        INSERT INTO public.users (name, email, password)
        VALUES ($1, $2, $3) RETURNING id
        """,
        "Eval Runner Test User", email, "test-password-hash",
    )
    try:
        yield user_id
    finally:
        # Best effort; if any golden_questions reference this user
        # via FK RESTRICT, skip the delete (matches doc-phase 124
        # pattern).
        try:
            await conn.execute(
                "DELETE FROM public.users WHERE id = $1", user_id
            )
        except asyncpg.ForeignKeyViolationError:
            pass


@pytest.fixture
async def synthetic_active_question(conn, synthetic_user):
    """Insert a synthetic active golden_questions row.

    Uses a UUID prefix in question_text so the stable_question_id
    composition (if any) won't collide with the production seed,
    mirroring the doc-phase 124 isolation pattern.
    """
    prefix = uuid4().hex[:8]
    question_id = await conn.fetchval(
        """
        INSERT INTO eval.golden_questions (
            question_set, question_text, context_setup,
            expected_intent_class, expected_citations,
            expected_entities, expected_numeric_values,
            expected_refusal, expected_language_compliance,
            difficulty, authored_by_user_id, status
        )
        VALUES (
            'ocr_triage', $1, '{}'::jsonb,
            NULL, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
            false, '[]'::jsonb, 'easy', $2, 'active'
        )
        RETURNING question_id
        """,
        f"[{prefix}] Synthetic OCR triage question",
        synthetic_user,
    )
    try:
        yield question_id
    finally:
        # run_results has ON DELETE CASCADE on question_id so this
        # tears down any results pointing at it.
        await conn.execute(
            "DELETE FROM eval.golden_questions WHERE question_id = $1::uuid",
            str(question_id),
        )


@pytest.fixture
async def cleanup_runs(conn):
    """Track + clean up run_summaries (and their results) created during a test.

    eval.run_results has no FK to eval.run_summaries, so orphans
    would linger across test runs if we only deleted the summaries.
    """
    created: list[UUID] = []
    yield created
    for rid in created:
        await conn.execute(
            "DELETE FROM eval.run_results WHERE run_id = $1::uuid", str(rid)
        )
        await conn.execute(
            "DELETE FROM eval.run_summaries WHERE run_id = $1::uuid", str(rid)
        )


# ----------------------------------------------------------------------
# Per-question evaluator stub tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_evaluate_question_synthetic_stub_passes(conn):
    """The synthetic stub returns passed=True with stub tag."""
    q = QuestionRecord(
        question_id=uuid4(),
        question_set="ocr_triage",
        question_text="dummy",
        context_setup={},
        expected_intent_class=None,
        expected_citations=[],
        expected_entities=[],
        expected_numeric_values=[],
        expected_refusal=False,
        expected_refusal_reason=None,
        expected_language_compliance=[],
        difficulty="easy",
    )
    result = await evaluate_question(conn, q)
    assert isinstance(result, QuestionResult)
    assert result.passed is True
    assert result.failure_layer is None
    assert result.actual_payload["evaluator"] == "synthetic_stub"
    assert result.actual_payload["doc_phase"] == 132
    assert result.latency_ms is not None and result.latency_ms >= 0


# ----------------------------------------------------------------------
# Promotion-gate tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_check_promotion_gate_clean_run_passes():
    """No fails / no regressions → no block, no reasons."""
    r = await check_promotion_gate(
        {"pass_count": 45, "fail_count": 0, "regression_count": 0}
    )
    assert r["blocks_promotion"] is False
    assert r["would_block"] is False
    assert r["reasons"] == []


@pytest.mark.asyncio
async def test_check_promotion_gate_warning_only_does_not_block():
    """Even if thresholds breach, warning_only mode never blocks."""
    t = RegressionThresholds(mode="warning_only", max_regression_count=0)
    r = await check_promotion_gate(
        {"pass_count": 10, "fail_count": 3, "regression_count": 3},
        thresholds=t,
    )
    assert r["blocks_promotion"] is False
    assert r["would_block"] is True  # gate would block in blocking mode
    assert any("regression_count" in s for s in r["reasons"])
    assert r["mode"] == "warning_only"


@pytest.mark.asyncio
async def test_check_promotion_gate_blocking_mode_trips_on_regression():
    """Blocking mode + over-cap regression → blocks_promotion=true."""
    t = RegressionThresholds(mode="blocking", max_regression_count=1)
    r = await check_promotion_gate(
        {"pass_count": 10, "fail_count": 3, "regression_count": 3},
        thresholds=t,
    )
    assert r["blocks_promotion"] is True
    assert r["would_block"] is True
    assert any("regression_count" in s for s in r["reasons"])


@pytest.mark.asyncio
async def test_check_promotion_gate_per_set_regression_trips():
    """Per-set regression cap fires even when global is clean."""
    t = RegressionThresholds(
        mode="blocking",
        max_regression_count=99,
        per_set_max_regression={"public_private_boundary": 0},
    )
    r = await check_promotion_gate(
        {
            "pass_count": 9, "fail_count": 1, "regression_count": 1,
            "per_set": {
                "public_private_boundary": {"pass": 0, "fail": 1, "regression": 1},
            },
        },
        thresholds=t,
    )
    assert r["blocks_promotion"] is True
    assert any("public_private_boundary" in s for s in r["reasons"])


# ----------------------------------------------------------------------
# End-to-end orchestration tests (touch the DB)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_workspace_evaluation_minimal_end_to_end(
    pool, conn, cleanup_runs, synthetic_active_question
):
    """Smoke: filter to one synthetic question, run, verify all writes."""
    result = await run_workspace_evaluation(
        triggered_by="manual",
        trigger_payload={"test": "doc-phase 132 smoke"},
        question_set_filter="ocr_triage",
        blocks_promotion=False,
        pool=pool,
    )
    cleanup_runs.append(result.run_id)

    assert isinstance(result.run_id, UUID)
    assert result.success is True
    assert result.question_count >= 1
    assert result.pass_count == result.question_count
    assert result.fail_count == 0
    assert result.regression_count == 0
    assert result.promotion_blocked is False

    # run_summaries row exists with completed_at set.
    summary = await conn.fetchrow(
        """
        SELECT triggered_by, question_count, pass_count, fail_count,
               regression_count, blocks_promotion, started_at, completed_at
          FROM eval.run_summaries
         WHERE run_id = $1::uuid
        """,
        str(result.run_id),
    )
    assert summary is not None
    assert summary["triggered_by"] == "manual"
    assert summary["question_count"] == result.question_count
    assert summary["pass_count"] == result.pass_count
    assert summary["completed_at"] is not None
    assert summary["completed_at"] >= summary["started_at"]

    # At least one run_results row written with synthetic_stub payload.
    row = await conn.fetchrow(
        """
        SELECT passed, actual_payload, failure_layer
          FROM eval.run_results
         WHERE run_id = $1::uuid AND question_id = $2::uuid
        """,
        str(result.run_id),
        str(synthetic_active_question),
    )
    assert row is not None
    assert row["passed"] is True
    payload = json.loads(row["actual_payload"]) if isinstance(row["actual_payload"], str) else row["actual_payload"]
    assert payload["evaluator"] == "synthetic_stub"
    assert row["failure_layer"] is None

    # Audit anchor emitted.
    audit_count = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'eval.run.complete'
           AND target_id = $1
        """,
        str(result.run_id),
    )
    assert audit_count == 1


@pytest.mark.asyncio
async def test_run_workspace_evaluation_regression_detection(
    pool, conn, cleanup_runs, synthetic_active_question
):
    """Insert a synthetic prior failing result, run, verify regression count.

    Manual prior-result injection simulates a failing baseline; the
    synthetic stub passes, so prior=False/current=True → NOT a
    regression (regressions are pass→fail, not fail→pass).

    We then flip: simulate prior=passed → current would have to fail
    to be a regression. Since the stub always passes, we cannot
    trigger a regression with the stub. This test asserts the
    happy-path: a prior passing result + a current passing result
    → regression_count=0 (i.e., the prior-lookup query joins
    correctly).
    """
    # Insert a prior run_summaries + run_results row with passed=True.
    prior_run_id = await conn.fetchval(
        """
        INSERT INTO eval.run_summaries (
            triggered_by, trigger_payload, question_count,
            pass_count, fail_count, regression_count, started_at,
            completed_at, blocks_promotion
        )
        VALUES ('manual', '{}'::jsonb, 1, 1, 0, 0,
                now() - interval '1 hour', now() - interval '59 minutes', false)
        RETURNING run_id
        """
    )
    cleanup_runs.append(prior_run_id)
    await conn.execute(
        """
        INSERT INTO eval.run_results (
            run_id, question_id, passed, actual_payload
        )
        VALUES ($1::uuid, $2::uuid, true, '{"evaluator":"synthetic_stub"}'::jsonb)
        """,
        str(prior_run_id),
        str(synthetic_active_question),
    )

    # Now run a fresh evaluation; current=passed, prior=passed → no regression.
    result = await run_workspace_evaluation(
        triggered_by="manual",
        question_set_filter="ocr_triage",
        pool=pool,
    )
    cleanup_runs.append(result.run_id)
    assert result.regression_count == 0


@pytest.mark.asyncio
async def test_run_workspace_evaluation_no_questions_for_filter(
    pool, conn, cleanup_runs
):
    """Run with a filter that matches no questions → empty run summary."""
    nonexistent_filter = "core_chat"  # may or may not have questions
    # Snapshot the count first; if there are real core_chat Qs we skip the assertion shape.
    n = await conn.fetchval(
        "SELECT count(*) FROM eval.golden_questions WHERE status='active' AND question_set='core_chat'"
    )
    result = await run_workspace_evaluation(
        triggered_by="manual",
        question_set_filter=nonexistent_filter,
        pool=pool,
    )
    cleanup_runs.append(result.run_id)
    assert result.question_count == int(n)
    assert result.pass_count == int(n)
    assert result.fail_count == 0
    assert result.regression_count == 0
