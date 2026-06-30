"""Nightly real_rag_v1 eval cron (§10.6) — doc-phase 170.

Schedule: ``15 5 * * *`` UTC (after flow_jwt_key_reaper @ 04:00 and any
phase0 5:00 agent; 15-min offset keeps the slot uncontended).

Fires `real_rag_v1` against the `refusal_correctness` question set
nightly. Builds on the doc-phase 169 graduation that wired the BGE
embedding model into the eval AgentDeps singleton — so the cron now
exercises the *full* §04i 6-layer chain end-to-end with real retrieval
+ real vLLM + real refusal validators every 24 h.

The cron is the §10.6 promotion-gate regression alarm:
  - 8 refusal_correctness questions seeded (doc-phase 160)
  - All 6 §04i layers chained (doc-phase 168, full chain green)
  - blocks_promotion=True → any regression flips `success=False` on the
    workflow output so a downstream gate / dashboard can pause auto-
    promotion of model/prompt changes.

Why a separate workflow instead of cron-attaching `evaluate_workspace`?

The existing `evaluate_workspace` workflow requires `eval_request_id`
as a non-default field — Hatchet cron-firing it would fail validation
because cron carries no payload. This wrapper generates a fresh
idempotency key per fire + fixes the evaluator/question-set defaults
for the nightly flavor, while leaving `evaluate_workspace` callable
ad-hoc from the Kestra / manual / promotion_gate paths.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet
from app.services.eval.workspace_evaluator import run_workspace_evaluation

log = logging.getLogger("georag.hatchet.eval_real_rag_nightly")


class EvalRealRagNightlyInput(BaseModel):
    """Optional overrides — defaults match the cron-fire path.

    Manual invocation (``eval_real_rag_nightly.run({"question_set_filter":
    "numeric_grounding"})``) lets the SME re-target the cron flavor
    against a different set without touching code.
    """

    question_set_filter: str = Field(
        default="refusal_correctness",
        description="Which §10.2 mechanical question set to exercise. "
                    "Default 'refusal_correctness' is the 8-question §2.9 "
                    "drift alarm set seeded doc-phase 160.",
    )
    blocks_promotion: bool = Field(
        default=True,
        description="If True (default), any regression flips success=False "
                    "on the output — downstream promotion gates pause.",
    )


class EvalRealRagNightlyOutput(BaseModel):
    run_id: UUID
    success: bool
    question_count: int
    pass_count: int
    fail_count: int
    regression_count: int
    promotion_blocked: bool
    failure_summary: str | None = None
    evaluator_kind: str = "real_rag_v1"
    question_set_filter: str = "refusal_correctness"
    # Doc-phase 175 — when the cron returns success=False, the workflow
    # emits an `eval.regression_detected` row to audit.audit_ledger.
    # Downstream Kestra flows / operator alerting subscribe to
    # that action_type. This field carries the resulting audit row's
    # id so observability tooling can correlate the cron output back to
    # the alarm event.
    regression_audit_id: str | None = None


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _emit_regression_alarm(
    *,
    run_id: UUID,
    eval_request_id: UUID,
    workflow_run_id: str | None,
    regression_count: int,
    fail_count: int,
    question_count: int,
    failure_summary: str | None,
    question_set_filter: str,
    promotion_blocked: bool,
) -> str | None:
    """Doc-phase 175 — emit an audit-ledger row for the alarm.

    Pattern matches the existing external_notification flow: audit
    ledger is the canonical notification surface, downstream
    Kestra flows poll by action_type. Emitting here keeps the
    alarm in the same tamper-evident hash chain as everything else.

    Returns the audit row's id, or None if emission fails (logged but
    non-fatal — the cron's primary signal is the workflow output).
    """
    try:
        conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
        try:
            entry = await emit_audit(
                conn,
                action_type="eval.regression_detected",
                workspace_id=None,  # platform-level alarm, not workspace-scoped
                actor_id=None,
                actor_kind="workflow",
                target_schema="eval",
                target_table="run_summaries",
                target_id=str(run_id),
                payload={
                    "eval_run_id": str(run_id),
                    "eval_request_id": str(eval_request_id),
                    "evaluator_kind": "real_rag_v1",
                    "question_set_filter": question_set_filter,
                    "question_count": question_count,
                    "fail_count": fail_count,
                    "regression_count": regression_count,
                    "promotion_blocked": promotion_blocked,
                    "failure_summary": failure_summary,
                    "cron_origin": "eval_real_rag_nightly",
                    "doc_phase": 175,
                },
                trace_id=workflow_run_id,
            )
            return str(entry.id)
        finally:
            await conn.close()
    except Exception as e:
        log.warning(
            "eval_real_rag_nightly.regression_alarm_emit_failed "
            "run_id=%s err=%s (non-fatal — workflow output still carries signal)",
            run_id, e,
        )
        return None


eval_real_rag_nightly = hatchet.workflow(
    name="eval_real_rag_nightly",
    on_crons=["15 5 * * *"],
    input_validator=EvalRealRagNightlyInput,
)


@eval_real_rag_nightly.task(execution_timeout="30m", retries=0)
async def run_nightly(
    input: EvalRealRagNightlyInput, ctx: Context
) -> EvalRealRagNightlyOutput:
    """Fire the §10.4 real-RAG eval suite once.

    Cron-fires with default `EvalRealRagNightlyInput()`. Manually
    invokable to override question_set_filter / blocks_promotion.
    """
    eval_request_id = uuid4()
    trigger_payload: dict[str, Any] = {
        "flavor": "nightly_real_rag",
        "evaluator_kind": "real_rag_v1",
        "question_set": input.question_set_filter,
        "doc_phase": 170,
    }
    log.info(
        "eval_real_rag_nightly.fired eval_request_id=%s question_set=%s "
        "blocks_promotion=%s",
        eval_request_id, input.question_set_filter, input.blocks_promotion,
    )
    result = await run_workspace_evaluation(
        triggered_by="cron",
        trigger_payload=trigger_payload,
        question_set_filter=input.question_set_filter,
        blocks_promotion=input.blocks_promotion,
        eval_request_id=eval_request_id,
        evaluator_kind="real_rag_v1",
    )
    log.info(
        "eval_real_rag_nightly.completed run_id=%s success=%s "
        "pass=%d fail=%d regressions=%d",
        result.run_id, result.success, result.pass_count,
        result.fail_count, result.regression_count,
    )

    # Doc-phase 175 — alarm emission on regression.
    # The cron's success=False means at least one regression was detected
    # (a previously-passing question now fails). Emit an audit-ledger row
    # that downstream notification flows (Kestra) subscribe to.
    regression_audit_id: str | None = None
    if not result.success and result.regression_count > 0:
        log.warning(
            "eval_real_rag_nightly.regression_detected run_id=%s "
            "regressions=%d fail=%d/%d — emitting alarm to audit ledger",
            result.run_id, result.regression_count,
            result.fail_count, result.question_count,
        )
        regression_audit_id = await _emit_regression_alarm(
            run_id=result.run_id,
            eval_request_id=eval_request_id,
            workflow_run_id=ctx.workflow_run_id,
            regression_count=result.regression_count,
            fail_count=result.fail_count,
            question_count=result.question_count,
            failure_summary=result.failure_summary,
            question_set_filter=input.question_set_filter,
            promotion_blocked=result.promotion_blocked,
        )

    # Phase 5 admin surface push — drives Admin/EvalDashboard.
    try:
        import logging

        from app.services.laravel_bridge import post_admin_surface_updated
        admin_payload = {
            "workflow_kind": "eval_real_rag_nightly",
            "run_id": str(result.run_id),
            "success": result.success,
            "question_count": result.question_count,
            "pass_count": result.pass_count,
            "fail_count": result.fail_count,
            "regression_count": result.regression_count,
            "promotion_blocked": result.promotion_blocked,
            "status": "success" if result.success else "failure",
        }
        await post_admin_surface_updated(
            surface="workflow-runs",
            affected_props=["workflow_runs"],
            payload=admin_payload,
        )
        await post_admin_surface_updated(
            surface="eval-dashboard",
            affected_props=["recent_runs", "kpis", "failure_layer_breakdown"],
            payload=admin_payload,
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "eval_real_rag_nightly: admin surface broadcasts failed run_id=%s err=%s",
            result.run_id, exc,
        )

    return EvalRealRagNightlyOutput(
        run_id=result.run_id,
        success=result.success,
        question_count=result.question_count,
        pass_count=result.pass_count,
        fail_count=result.fail_count,
        regression_count=result.regression_count,
        promotion_blocked=result.promotion_blocked,
        failure_summary=result.failure_summary,
        evaluator_kind="real_rag_v1",
        question_set_filter=input.question_set_filter,
        regression_audit_id=regression_audit_id,
    )


__all__ = [
    "eval_real_rag_nightly",
    "EvalRealRagNightlyInput",
    "EvalRealRagNightlyOutput",
]
