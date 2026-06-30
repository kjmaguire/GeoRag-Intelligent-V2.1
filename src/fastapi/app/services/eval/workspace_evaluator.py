"""Workspace eval orchestration (§10.4) — doc-phase 132.

Live orchestration body for the `evaluate_workspace` Hatchet workflow.
Implements the per-question fanout + result aggregation + promotion
gate evaluation. The Hatchet task body in
`app.hatchet_workflows.evaluate_workspace.execute` is a thin wrapper
that calls `run_workspace_evaluation()` here.

What's live in this graduation (doc-phase 132):

  - `run_workspace_evaluation()` — async function that:
      1. Creates a row in `eval.run_summaries` for the new run
      2. Loads active `eval.golden_questions` (optionally filter by
         question_set)
      3. For each question, calls `evaluate_question()` and writes
         a row to `eval.run_results`
      4. Computes `regression_count` by comparing each question's
         current result against its most recent prior run result
      5. Updates `eval.run_summaries` with final counts +
         `completed_at`
      6. Calls `check_promotion_gate()` (graduated alongside) to
         decide `promotion_blocked`
      7. Emits an `eval.run.complete` audit ledger anchor

  - `evaluate_question()` — pluggable evaluator. Today returns a
    **synthetic deterministic stub** (every active question passes
    with `actual_payload={"evaluator": "synthetic_stub", ...}`). The
    real RAG/LLM evaluator is a future graduation that swaps this
    function out; the orchestration above does not change.

The synthetic stub is honest: every result row carries
`actual_payload.evaluator == "synthetic_stub"` so the Eval Dashboard
can mark its rows clearly until the real evaluator lands.

Idempotency: `eval.run_results` has a UNIQUE(run_id, question_id)
constraint. Re-running the same `run_id` against the same question
set raises asyncpg.UniqueViolation. The caller (Hatchet) keys on
`eval_request_id`, so distinct workflow runs always get fresh
`run_id`s.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, NamedTuple
from uuid import UUID

import asyncpg

from app.audit import emit_audit
from app.services.eval.thresholds import (
    DEFAULT_REGRESSION_THRESHOLDS,
    RegressionThresholds,
    check_promotion_gate,
)

log = logging.getLogger("georag.eval.workspace_evaluator")


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


class QuestionRecord(NamedTuple):
    """Subset of `eval.golden_questions` the evaluator needs."""

    question_id: UUID
    question_set: str
    question_text: str
    context_setup: dict[str, Any]
    expected_intent_class: str | None
    expected_citations: list[Any]
    expected_entities: list[Any]
    expected_numeric_values: list[Any]
    expected_refusal: bool
    expected_refusal_reason: str | None
    expected_language_compliance: list[Any]
    difficulty: str


class QuestionResult(NamedTuple):
    """Result tuple from `evaluate_question`."""

    passed: bool
    actual_payload: dict[str, Any]
    failure_layer: str | None  # 'setup' | 'retrieval' | 'citation' | 'numeric' | 'refusal' | 'language' | 'evaluator_not_ready' | None
    failure_detail: str | None
    latency_ms: int | None
    tokens_used: int | None


class WorkspaceEvaluationResult(NamedTuple):
    """Final aggregate returned by `run_workspace_evaluation`."""

    run_id: UUID
    success: bool
    question_count: int
    pass_count: int
    fail_count: int
    regression_count: int
    promotion_blocked: bool
    failure_summary: str | None


async def evaluate_question(
    conn: asyncpg.Connection,
    question: QuestionRecord,
) -> QuestionResult:
    """Per-question evaluator — doc-phase 132 synthetic stub.

    Returns a deterministic synthetic result. Every active question
    "passes" with `actual_payload` clearly tagged as a stub. The real
    evaluator (planned: §04i hallucination prevention pipeline against
    the workspace's RAG) replaces this function without touching the
    surrounding orchestration.

    Args:
        conn: asyncpg connection (unused by stub; real evaluator
            uses it for context_setup application).
        question: golden_questions row.

    Returns:
        QuestionResult — `passed=True`, `actual_payload` carrying the
        evaluator tag, `failure_layer=None`.
    """
    t_start = time.monotonic()
    # Stub: in real evaluator this is where we'd:
    #   1. Apply context_setup (set GUCs / load fixture chunks)
    #   2. Call the RAG pipeline with question_text
    #   3. Run 6-layer hallucination prevention (§04i)
    #   4. Compare actual vs expected_citations/entities/numeric/etc.
    actual_payload: dict[str, Any] = {
        "evaluator": "synthetic_stub",
        "doc_phase": 132,
        "question_set": question.question_set,
        "difficulty": question.difficulty,
        "note": "Real RAG/LLM evaluator graduates with §04i wiring.",
    }
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return QuestionResult(
        passed=True,
        actual_payload=actual_payload,
        failure_layer=None,
        failure_detail=None,
        latency_ms=elapsed_ms,
        tokens_used=0,
    )


async def _load_active_questions(
    conn: asyncpg.Connection,
    question_set_filter: str | None,
) -> list[QuestionRecord]:
    """Fetch active golden questions, optionally filtered by question_set."""
    if question_set_filter is not None:
        rows = await conn.fetch(
            """
            SELECT question_id, question_set, question_text, context_setup,
                   expected_intent_class, expected_citations, expected_entities,
                   expected_numeric_values, expected_refusal,
                   expected_refusal_reason, expected_language_compliance,
                   difficulty
              FROM eval.golden_questions
             WHERE status = 'active' AND question_set = $1
             ORDER BY question_set, question_id
            """,
            question_set_filter,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT question_id, question_set, question_text, context_setup,
                   expected_intent_class, expected_citations, expected_entities,
                   expected_numeric_values, expected_refusal,
                   expected_refusal_reason, expected_language_compliance,
                   difficulty
              FROM eval.golden_questions
             WHERE status = 'active'
             ORDER BY question_set, question_id
            """
        )

    out: list[QuestionRecord] = []
    for r in rows:
        out.append(
            QuestionRecord(
                question_id=r["question_id"],
                question_set=r["question_set"],
                question_text=r["question_text"],
                context_setup=json.loads(r["context_setup"]) if isinstance(r["context_setup"], str) else (r["context_setup"] or {}),
                expected_intent_class=r["expected_intent_class"],
                expected_citations=json.loads(r["expected_citations"]) if isinstance(r["expected_citations"], str) else (r["expected_citations"] or []),
                expected_entities=json.loads(r["expected_entities"]) if isinstance(r["expected_entities"], str) else (r["expected_entities"] or []),
                expected_numeric_values=json.loads(r["expected_numeric_values"]) if isinstance(r["expected_numeric_values"], str) else (r["expected_numeric_values"] or []),
                expected_refusal=r["expected_refusal"],
                expected_refusal_reason=r["expected_refusal_reason"],
                expected_language_compliance=json.loads(r["expected_language_compliance"]) if isinstance(r["expected_language_compliance"], str) else (r["expected_language_compliance"] or []),
                difficulty=r["difficulty"],
            )
        )
    return out


async def _prior_passed_outcomes(
    conn: asyncpg.Connection,
    run_id: UUID,
    question_ids: list[UUID],
) -> dict[UUID, bool]:
    """For each question, fetch its most recent prior result's pass/fail.

    "Prior" = most recent `eval.run_results` row for this question
    whose `executed_at` is strictly earlier than the current run's
    `started_at`, ignoring the current run_id. Used to compute
    regression_count.

    Returns {question_id -> passed} for questions that have a prior
    result. Questions without history don't appear in the map (no
    baseline → can't regress).
    """
    if not question_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (r.question_id) r.question_id, r.passed
          FROM eval.run_results r
          JOIN eval.run_summaries s ON s.run_id = r.run_id
         WHERE r.question_id = ANY($1::uuid[])
           AND r.run_id <> $2::uuid
           AND s.started_at < (SELECT started_at FROM eval.run_summaries WHERE run_id = $2::uuid)
         ORDER BY r.question_id, s.started_at DESC
        """,
        [str(q) for q in question_ids],
        str(run_id),
    )
    return {r["question_id"]: r["passed"] for r in rows}


async def run_workspace_evaluation(
    *,
    triggered_by: str,
    trigger_payload: dict[str, Any] | None = None,
    question_set_filter: str | None = None,
    blocks_promotion: bool = False,
    eval_request_id: UUID | None = None,
    thresholds: RegressionThresholds | None = None,
    pool: asyncpg.Pool | None = None,
    evaluator_kind: str = "synthetic_stub",
) -> WorkspaceEvaluationResult:
    """Execute the full eval orchestration end-to-end.

    Args:
        triggered_by: 'cron' | 'manual' | 'promotion_gate' | 'prompt_change'.
            Matches `eval.run_summaries.triggered_by` CHECK constraint.
        trigger_payload: free-form context payload stored on the
            run_summaries row.
        question_set_filter: if provided, only run questions in this
            set. Otherwise all active questions.
        blocks_promotion: if true, regression breaches return
            success=false.
        eval_request_id: optional idempotency key (informational —
            we don't dedupe on it; the caller does).
        thresholds: optional override of regression thresholds.
        pool: optional asyncpg pool to reuse (tests pass one in).
        evaluator_kind: 'synthetic_stub' (doc-phase 132 default) or
            'real_llm_v1' (doc-phase 159; calls vLLM + applies
            refusal-correctness validator).

    Returns:
        WorkspaceEvaluationResult with run_id + counts +
        promotion_blocked flag.
    """
    # Doc-phase 159+162 — pick the per-question evaluator function:
    #   - 'synthetic_stub' (doc-phase 132 default; always-pass)
    #   - 'real_llm_v1'    (doc-phase 159; vLLM only, refusal validator)
    #   - 'real_rag_v1'    (doc-phase 162; full RAG + refusal validator)
    if evaluator_kind == "real_rag_v1":
        from app.services.eval.real_rag_evaluator import (
            evaluate_question_real_rag,
        )
        per_question_evaluator = evaluate_question_real_rag
    elif evaluator_kind == "real_llm_v1":
        from app.services.eval.real_llm_evaluator import (
            evaluate_question_real_llm,
        )
        per_question_evaluator = evaluate_question_real_llm
    elif evaluator_kind == "synthetic_stub":
        per_question_evaluator = evaluate_question
    else:
        raise ValueError(
            f"unknown evaluator_kind={evaluator_kind!r}; "
            f"valid: 'synthetic_stub' | 'real_llm_v1' | 'real_rag_v1'"
        )
    thresholds = thresholds or DEFAULT_REGRESSION_THRESHOLDS
    trigger_payload = dict(trigger_payload or {})
    # Doc-phase 164 — persist evaluator_kind into trigger_payload so the
    # Eval Dashboard's recent-runs table can surface which evaluator
    # ran each row.
    trigger_payload.setdefault("evaluator_kind", evaluator_kind)
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        async with pool.acquire() as conn:
            # 1. Insert run_summaries row (starts the run).
            run_id_row = await conn.fetchrow(
                """
                INSERT INTO eval.run_summaries (
                    triggered_by, trigger_payload, question_set_filter,
                    blocks_promotion, question_count, pass_count,
                    fail_count, regression_count
                )
                VALUES ($1, $2::jsonb, $3, $4, 0, 0, 0, 0)
                RETURNING run_id, started_at
                """,
                triggered_by,
                json.dumps(trigger_payload, default=str, sort_keys=True),
                question_set_filter,
                blocks_promotion,
            )
            run_id: UUID = run_id_row["run_id"]
            log.info(
                "evaluate_workspace.run_started run_id=%s triggered_by=%s "
                "filter=%s blocks_promotion=%s",
                run_id, triggered_by, question_set_filter, blocks_promotion,
            )

            # 2. Load active questions.
            questions = await _load_active_questions(
                conn, question_set_filter
            )
            question_count = len(questions)

            # 3. Per-question fanout (sequential for v1; safe for the
            #    synthetic stub. Real evaluator will use asyncio.gather
            #    with a Semaphore to bound concurrency).
            per_question: dict[UUID, QuestionResult] = {}
            for q in questions:
                result = await per_question_evaluator(conn, q)
                per_question[q.question_id] = result
                await conn.execute(
                    """
                    INSERT INTO eval.run_results (
                        run_id, question_id, passed, actual_payload,
                        failure_layer, failure_detail, latency_ms,
                        tokens_used
                    )
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
                    """,
                    str(run_id),
                    str(q.question_id),
                    result.passed,
                    json.dumps(result.actual_payload, default=str, sort_keys=True),
                    result.failure_layer,
                    result.failure_detail,
                    result.latency_ms,
                    result.tokens_used,
                )

            # 4. Aggregate.
            pass_count = sum(1 for r in per_question.values() if r.passed)
            fail_count = question_count - pass_count

            # 5. Regression detection: a question regresses if it
            #    passed previously but failed this run.
            prior = await _prior_passed_outcomes(
                conn, run_id, list(per_question.keys())
            )
            regression_count = sum(
                1
                for qid, result in per_question.items()
                if not result.passed and prior.get(qid) is True
            )

            # 6. Update run_summaries with finals.
            await conn.execute(
                """
                UPDATE eval.run_summaries
                   SET question_count = $1,
                       pass_count = $2,
                       fail_count = $3,
                       regression_count = $4,
                       completed_at = now()
                 WHERE run_id = $5
                """,
                question_count, pass_count, fail_count, regression_count,
                str(run_id),
            )

            # 7. Promotion gate.
            per_set_breakdown: dict[str, dict[str, int]] = {}
            for q in questions:
                bucket = per_set_breakdown.setdefault(
                    q.question_set,
                    {"pass": 0, "fail": 0, "regression": 0},
                )
                result = per_question[q.question_id]
                if result.passed:
                    bucket["pass"] += 1
                else:
                    bucket["fail"] += 1
                    if prior.get(q.question_id) is True:
                        bucket["regression"] += 1

            gate = await check_promotion_gate(
                {
                    "run_id": str(run_id),
                    "question_count": question_count,
                    "pass_count": pass_count,
                    "fail_count": fail_count,
                    "regression_count": regression_count,
                    "blocks_promotion": blocks_promotion,
                    "per_set": per_set_breakdown,
                },
                thresholds=thresholds,
            )
            promotion_blocked = bool(gate.get("blocks_promotion", False))

            # 8. Audit anchor.
            await emit_audit(
                conn,
                action_type="eval.run.complete",
                actor_kind="system",
                target_schema="eval",
                target_table="run_summaries",
                target_id=str(run_id),
                payload={
                    "triggered_by": triggered_by,
                    "question_count": question_count,
                    "pass_count": pass_count,
                    "fail_count": fail_count,
                    "regression_count": regression_count,
                    "promotion_blocked": promotion_blocked,
                    "evaluator": "synthetic_stub",
                    "doc_phase": 132,
                },
            )

            # 9. Build outcome.
            #    success=false iff (a) blocks_promotion is set and
            #    the gate would block, OR (b) any question failed
            #    in blocking mode. We mirror Hatchet caller semantics.
            success = not (blocks_promotion and promotion_blocked)
            failure_summary: str | None = None
            if not success:
                reasons = gate.get("reasons", [])
                failure_summary = (
                    f"Promotion blocked: {'; '.join(reasons) if reasons else 'unknown reason'}"
                )

            log.info(
                "evaluate_workspace.run_completed run_id=%s pass=%d fail=%d "
                "regressions=%d promotion_blocked=%s success=%s",
                run_id, pass_count, fail_count, regression_count,
                promotion_blocked, success,
            )
            return WorkspaceEvaluationResult(
                run_id=run_id,
                success=success,
                question_count=question_count,
                pass_count=pass_count,
                fail_count=fail_count,
                regression_count=regression_count,
                promotion_blocked=promotion_blocked,
                failure_summary=failure_summary,
            )
    finally:
        if owns_pool and pool is not None:
            await pool.close()


__all__ = [
    "QuestionRecord",
    "QuestionResult",
    "WorkspaceEvaluationResult",
    "evaluate_question",
    "run_workspace_evaluation",
]
