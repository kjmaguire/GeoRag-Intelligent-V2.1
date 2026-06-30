"""Tests for the doc-phase 184 field_outcome_learning graduation."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.hatchet_workflows.field_outcome_learning import (
    FieldOutcomeLearningInput,
)
from app.hatchet_workflows.field_outcome_learning import (
    execute as field_outcome_task,
)


@pytest.mark.asyncio
async def test_field_outcome_learning_no_outcomes_returns_success():
    """Empty outcome set → success=True with zero counts. Verifies the
    graduation cleanly handles the "no new outcomes" common case."""
    inp = FieldOutcomeLearningInput(
        workspace_id=uuid4(),
        project_id=uuid4(),
        outcome_ids=[],
    )
    # Use aio_mock_run to invoke without Hatchet engine
    out = await field_outcome_task.aio_mock_run(inp)
    assert out.success is True
    assert out.outcomes_processed == 0
    assert out.backtests_written == 0
    assert out.retraining_triggered is False


def test_field_outcome_learning_input_validation():
    """FieldOutcomeLearningInput requires workspace_id + project_id."""
    inp = FieldOutcomeLearningInput(
        workspace_id=uuid4(),
        project_id=uuid4(),
    )
    # outcome_ids defaults to empty
    assert inp.outcome_ids == []
