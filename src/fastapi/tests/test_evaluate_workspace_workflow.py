"""Live tests for the doc-phase 161 evaluator_kind threading in the
`evaluate_workspace` Hatchet workflow."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.hatchet_workflows.evaluate_workspace import (
    EvaluateWorkspaceInput,
    execute as evaluate_workspace_execute,
)


@pytest.mark.asyncio
async def test_evaluate_workspace_default_evaluator_is_synthetic_stub():
    """Backward-compat: an input without evaluator_kind defaults to
    synthetic_stub (which always passes whatever it sees)."""
    inp = EvaluateWorkspaceInput(
        triggered_by="manual",
        question_set_filter="core_chat",  # 0 active questions → empty run
        eval_request_id=uuid4(),
    )
    out = await evaluate_workspace_execute.aio_mock_run(inp)
    assert out.success is True
    assert out.evaluator_kind == "synthetic_stub"


@pytest.mark.asyncio
async def test_evaluate_workspace_real_llm_v1_threads_through():
    """evaluator_kind='real_llm_v1' is accepted + echoed in output."""
    inp = EvaluateWorkspaceInput(
        triggered_by="manual",
        question_set_filter="core_chat",  # empty set → no vLLM calls needed
        evaluator_kind="real_llm_v1",
        eval_request_id=uuid4(),
    )
    out = await evaluate_workspace_execute.aio_mock_run(inp)
    assert out.evaluator_kind == "real_llm_v1"
    assert out.success is True


@pytest.mark.asyncio
async def test_evaluate_workspace_rejects_invalid_evaluator_kind():
    """Pydantic Literal validation rejects unknown evaluator_kind."""
    with pytest.raises(Exception) as exc_info:  # ValidationError
        EvaluateWorkspaceInput(
            triggered_by="manual",
            evaluator_kind="not_a_real_evaluator",  # type: ignore[arg-type]
            eval_request_id=uuid4(),
        )
    assert "evaluator_kind" in str(exc_info.value).lower() or "literal" in str(exc_info.value).lower()
