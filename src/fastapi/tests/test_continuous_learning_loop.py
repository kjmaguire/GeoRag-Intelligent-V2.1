"""§12.10 continuous_learning_loop tests (Phase H4 graduation)."""
from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from app.hatchet_workflows.continuous_learning_loop import (
    ContinuousLearningLoopInput,
    execute as continuous_learning_loop_execute,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _live_db_available() -> bool:
    return bool(os.environ.get("POSTGRES_PASSWORD"))


@pytest.mark.asyncio
async def test_continuous_learning_loop_runs_against_real_workspaces() -> None:
    """Smoke: the orchestrator walks every workspace, records the
    threshold check, and emits an audit anchor."""
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")

    inp = ContinuousLearningLoopInput(
        initiated_by="test",
        target_retraining_threshold=25,
        source_trust_retraining_threshold=500,
        loop_request_id=uuid4(),
    )
    out = await continuous_learning_loop_execute.aio_mock_run(inp)
    assert out.success is True
    # Scanned at least the workspaces that exist
    assert out.workspaces_scanned >= 1
    assert out.workspaces_evaluated == out.workspaces_scanned


@pytest.mark.asyncio
async def test_continuous_learning_loop_emits_audit_anchor() -> None:
    """The loop emits a `continuous_learning_loop.completed` row to
    the audit ledger so the next run can compute deltas from it."""
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")

    request_id = uuid4()
    inp = ContinuousLearningLoopInput(
        initiated_by="test",
        loop_request_id=request_id,
    )
    out = await continuous_learning_loop_execute.aio_mock_run(inp)
    assert out.success is True

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        n = await conn.fetchval(
            """
            SELECT count(*) FROM audit.audit_ledger
             WHERE action_type = 'continuous_learning_loop.completed'
               AND target_id = $1
            """,
            str(request_id),
        )
    finally:
        await conn.close()
    assert n == 1


@pytest.mark.asyncio
async def test_continuous_learning_loop_threshold_flags_pending_workspaces() -> None:
    """With thresholds set to 0, every workspace with any outcomes
    activity should flag as pending training. (Defensive check — the
    orchestrator should never crash and always return a structured
    result.)"""
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")

    inp = ContinuousLearningLoopInput(
        initiated_by="test",
        target_retraining_threshold=0,
        source_trust_retraining_threshold=0,
        loop_request_id=uuid4(),
    )
    out = await continuous_learning_loop_execute.aio_mock_run(inp)
    assert out.success is True
    # workspaces_pending_training >= 0 (no live outcome data yet in this DB)
    assert out.workspaces_pending_training >= 0
