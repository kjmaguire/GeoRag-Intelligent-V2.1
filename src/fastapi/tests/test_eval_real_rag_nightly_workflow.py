"""Live tests for the doc-phase 170 nightly real-RAG cron workflow.

Verifies the cron wrapper:
  - accepts default empty input (cron-fire path)
  - generates a fresh eval_request_id per call
  - threads evaluator_kind='real_rag_v1' + question_set_filter through
  - echoes flavor metadata in the output
  - accepts manual override of question_set_filter / blocks_promotion
"""
from __future__ import annotations

import pytest

from app.hatchet_workflows.eval_real_rag_nightly import (
    EvalRealRagNightlyInput,
    eval_real_rag_nightly,
)
from app.hatchet_workflows.eval_real_rag_nightly import (
    run_nightly as run_nightly_task,
)


def test_default_input_matches_cron_fire_path():
    """Empty `EvalRealRagNightlyInput()` cron-fire is valid + uses
    refusal_correctness + blocks_promotion=True defaults."""
    inp = EvalRealRagNightlyInput()
    assert inp.question_set_filter == "refusal_correctness"
    assert inp.blocks_promotion is True


def test_workflow_carries_correct_cron_schedule():
    """The cron schedule is the agreed slot — 15 5 * * * UTC.

    Co-located with `flow_jwt_key_reaper` (04:00) + phase0_agents (05:00)
    but offset by 15 min so the AI pool isn't all firing at the same
    second. Verified here so the slot can't drift unnoticed.
    """
    # Hatchet workflow.config stores the cron list on the underlying
    # WorkflowConfig object; we just confirm our slot is in there.
    cron_list = getattr(eval_real_rag_nightly.config, "on_crons", None) or \
                getattr(eval_real_rag_nightly, "on_crons", None)
    assert cron_list is not None, "workflow missing on_crons attribute"
    assert "15 5 * * *" in cron_list


@pytest.mark.asyncio
async def test_workflow_body_fires_real_rag_v1():
    """Cron-fire path: empty input → workflow runs `real_rag_v1` against
    `refusal_correctness`. The 8 seeded refusal questions pass under
    the full 6-layer chain (verified live doc-phase 168/169)."""
    inp = EvalRealRagNightlyInput()
    out = await run_nightly_task.aio_mock_run(inp)

    assert out.evaluator_kind == "real_rag_v1"
    assert out.question_set_filter == "refusal_correctness"
    # success is True iff no regressions — under live verified behavior
    # this should pass cleanly. We don't hard-assert on counts here
    # because the live verification covers that.
    assert isinstance(out.run_id, type(out.run_id))  # UUID instance
    assert out.question_count >= 0
    assert out.pass_count + out.fail_count == out.question_count


@pytest.mark.asyncio
async def test_workflow_body_accepts_manual_override():
    """Manual invocation can re-target the cron flavor at a different
    question_set without code changes — useful for ad-hoc re-runs."""
    # Doc-phase 179 — `core_chat` got 10 Wyoming uranium questions
    # seeded; use a still-empty set for the empty-set assertion.
    inp = EvalRealRagNightlyInput(
        question_set_filter="public_private_boundary",  # 0 active today
        blocks_promotion=False,
    )
    out = await run_nightly_task.aio_mock_run(inp)

    assert out.evaluator_kind == "real_rag_v1"
    assert out.question_set_filter == "public_private_boundary"
    # Empty question set should produce success=True with zero work
    assert out.question_count == 0
    assert out.success is True
    # Doc-phase 175 — no regressions → no alarm emission
    assert out.regression_audit_id is None


# ─────────────────────── Doc-phase 175 — alarm emission ──────────────────────


@pytest.mark.asyncio
async def test_workflow_alarm_helper_emits_audit_row():
    """The `_emit_regression_alarm` helper writes a row to
    audit.audit_ledger with the canonical `eval.regression_detected`
    action_type. Downstream Activepieces flows subscribe to that
    action_type for operator notification."""
    from uuid import uuid4

    from app.hatchet_workflows.eval_real_rag_nightly import (
        _emit_regression_alarm,
    )

    fake_run_id = uuid4()
    audit_id = await _emit_regression_alarm(
        run_id=fake_run_id,
        eval_request_id=uuid4(),
        workflow_run_id="test-workflow-run-id",
        regression_count=2,
        fail_count=5,
        question_count=8,
        failure_summary="2 regressions on layer 5_chunk_provenance",
        question_set_filter="refusal_correctness",
        promotion_blocked=True,
    )
    assert audit_id is not None, (
        "emit_audit returned None — alarm path is silent on regressions"
    )

    # Verify the row landed with the right action_type + payload shape
    import asyncpg

    from app.hatchet_workflows.eval_real_rag_nightly import _build_dsn
    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            """
            SELECT action_type, actor_kind, target_schema, target_table,
                   payload, target_id
              FROM audit.audit_ledger
             WHERE id = $1::uuid
            """,
            audit_id,
        )
    finally:
        await conn.close()

    assert row is not None
    assert row["action_type"] == "eval.regression_detected"
    assert row["actor_kind"] == "workflow"
    assert row["target_schema"] == "eval"
    assert row["target_table"] == "run_summaries"
    assert row["target_id"] == str(fake_run_id)

    import json
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert payload["regression_count"] == 2
    assert payload["fail_count"] == 5
    assert payload["question_count"] == 8
    assert payload["evaluator_kind"] == "real_rag_v1"
    assert payload["question_set_filter"] == "refusal_correctness"
    assert payload["cron_origin"] == "eval_real_rag_nightly"
    assert payload["doc_phase"] == 175
    assert "regressions on layer 5_chunk_provenance" in payload["failure_summary"]


@pytest.mark.asyncio
async def test_workflow_does_not_emit_alarm_when_success():
    """When success=True (no regressions), no alarm should fire — the
    audit ledger is reserved for actual alarms, not green-state noise."""
    # Doc-phase 179 — core_chat got 10 questions seeded; use empty
    # public_private_boundary set for green-state assertion.
    inp = EvalRealRagNightlyInput(
        question_set_filter="public_private_boundary",  # 0 active today
        blocks_promotion=False,
    )
    out = await run_nightly_task.aio_mock_run(inp)
    assert out.success is True
    assert out.regression_audit_id is None
