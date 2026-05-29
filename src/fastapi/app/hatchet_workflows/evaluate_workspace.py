"""evaluate_workspace Hatchet workflow (§10.4 / §24.3).

Doc-phase 98 skeleton → doc-phase 132 graduation → doc-phase 161
adds `evaluator_kind` selection.

Runs the golden-question eval suite against a candidate model /
prompt configuration. Fans out per-question; each child writes a
row to `eval.run_results`. Parent rolls up to `eval.run_summaries`
with pass/fail/regression counts.

Per §10.6, if regression breaches threshold AND `blocks_promotion=true`,
the workflow returns success=false — the caller (promotion gate or
cron) enforces the block.

The task body is a thin wrapper that delegates all orchestration to
`app.services.eval.workspace_evaluator.run_workspace_evaluation`.
The per-question evaluator can be either:
  - 'synthetic_stub' (doc-phase 132 default; always-pass with stub tag)
  - 'real_llm_v1' (doc-phase 159; calls vLLM + applies §04i Layer 6
    refusal-correctness validator)

Selected via `EvaluateWorkspaceInput.evaluator_kind` so cron /
Kestra can dispatch real-LLM evals on a schedule while keeping
ad-hoc synthetic_stub runs for quick smoke checks.
"""
from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.eval.workspace_evaluator import run_workspace_evaluation


log = logging.getLogger("georag.hatchet.evaluate_workspace")


EvaluatorKind = Literal["synthetic_stub", "real_llm_v1", "real_rag_v1"]


class EvaluateWorkspaceInput(BaseModel):
    """Trigger payload — manual run, cron, promotion gate."""

    triggered_by: str = Field(
        ..., description="cron | manual | promotion_gate | prompt_change"
    )
    trigger_payload: dict[str, Any] = Field(default_factory=dict)
    question_set_filter: str | None = Field(
        default=None,
        description="Optional filter — only run questions in this set.",
    )
    blocks_promotion: bool = Field(
        default=False,
        description="If true, regression_count > 0 causes the workflow to "
                    "report success=false and block any downstream promotion.",
    )
    eval_request_id: UUID = Field(..., description="Idempotency key.")
    # Doc-phase 161 — let the caller pick the evaluator. Default keeps
    # backward compat (synthetic_stub from doc-phase 132).
    evaluator_kind: EvaluatorKind = Field(
        default="synthetic_stub",
        description="'synthetic_stub' (always-pass with tag) or "
                    "'real_llm_v1' (vLLM + §04i refusal-correctness "
                    "validator, doc-phase 159).",
    )


class EvaluateWorkspaceOutput(BaseModel):
    run_id: UUID
    success: bool
    question_count: int = 0
    pass_count: int = 0
    fail_count: int = 0
    regression_count: int = 0
    promotion_blocked: bool = False
    failure_summary: str | None = None
    # Doc-phase 161 — echo back which evaluator ran so the caller's
    # observability picks it up without re-querying audit anchors.
    evaluator_kind: EvaluatorKind = "synthetic_stub"


evaluate_workspace = hatchet.workflow(
    name="evaluate_workspace",
    input_validator=EvaluateWorkspaceInput,
)


@evaluate_workspace.task(execution_timeout="2h", retries=0)
async def execute(
    input: EvaluateWorkspaceInput, ctx: Context
) -> EvaluateWorkspaceOutput:
    """Run the golden-question eval suite end-to-end. Graduated doc-phase 132.

    Delegates to `run_workspace_evaluation` in
    `app.services.eval.workspace_evaluator`. The orchestration there
    is fully live; the per-question evaluator is a synthetic stub
    until the §04i RAG/LLM evaluator graduates.
    """
    log.info(
        "evaluate_workspace.task_started eval_request_id=%s triggered_by=%s "
        "evaluator_kind=%s",
        input.eval_request_id, input.triggered_by, input.evaluator_kind,
    )
    result = await run_workspace_evaluation(
        triggered_by=input.triggered_by,
        trigger_payload=input.trigger_payload,
        question_set_filter=input.question_set_filter,
        blocks_promotion=input.blocks_promotion,
        eval_request_id=input.eval_request_id,
        evaluator_kind=input.evaluator_kind,
    )

    # Phase 5 admin surface push — drives Admin/EvalDashboard.
    try:
        from app.services.laravel_bridge import post_admin_surface_updated
        admin_payload = {
            "workflow_kind": "evaluate_workspace",
            "run_id": str(result.run_id),
            "success": result.success,
            "question_count": result.question_count,
            "pass_count": result.pass_count,
            "fail_count": result.fail_count,
            "regression_count": result.regression_count,
            "promotion_blocked": result.promotion_blocked,
            "evaluator_kind": input.evaluator_kind,
            "status": "success" if result.success else "failure",
        }
        await post_admin_surface_updated(
            surface="workflow-runs",
            affected_props=["workflow_runs"],
            payload=admin_payload,
        )
        await post_admin_surface_updated(
            surface="eval-dashboard",
            affected_props=["recent_runs", "kpis"],
            payload=admin_payload,
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "evaluate_workspace: admin surface broadcasts failed run_id=%s err=%s",
            result.run_id, exc,
        )

    return EvaluateWorkspaceOutput(
        run_id=result.run_id,
        success=result.success,
        question_count=result.question_count,
        pass_count=result.pass_count,
        fail_count=result.fail_count,
        regression_count=result.regression_count,
        promotion_blocked=result.promotion_blocked,
        failure_summary=result.failure_summary,
        evaluator_kind=input.evaluator_kind,
    )
