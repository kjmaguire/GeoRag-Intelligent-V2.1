"""Live tests for the doc-phase 162 real RAG-backed evaluator.

Tests fall into three groups:
  1. Module import + structural tests (no RAG infrastructure needed)
  2. AgentDeps construction smoke (gated on Qdrant + Neo4j + at
     least one row in silver.projects)
  3. End-to-end refusal-question pass (gated on full RAG + vLLM
     reachability)
"""
from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

# Lazy import the evaluator to avoid pulling in the orchestrator at
# collection time (it imports the full app tooling graph).


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


async def _has_projects() -> bool:
    """Check whether silver.projects has at least one row."""
    try:
        c = await asyncpg.connect(_dsn(), statement_cache_size=0)
        try:
            n = await c.fetchval("SELECT count(*) FROM silver.projects")
            return n > 0
        finally:
            await c.close()
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# Module structure tests
# ----------------------------------------------------------------------
def test_real_rag_evaluator_exports():
    from app.services.eval.real_rag_evaluator import (
        evaluate_question_real_rag,
    )
    assert callable(evaluate_question_real_rag)


def test_workspace_evaluator_accepts_real_rag_v1_kind():
    """Pydantic Literal validation accepts the new value."""
    from app.hatchet_workflows.evaluate_workspace import EvaluateWorkspaceInput

    inp = EvaluateWorkspaceInput(
        triggered_by="manual",
        evaluator_kind="real_rag_v1",
        eval_request_id=uuid4(),
    )
    assert inp.evaluator_kind == "real_rag_v1"


def test_workspace_evaluator_rejects_unknown_kind_with_real_rag_message():
    """Error message names all 3 valid options."""
    from app.services.eval.workspace_evaluator import run_workspace_evaluation
    import asyncio
    with pytest.raises(ValueError, match="real_rag_v1"):
        # We expect the ValueError before any pool gets created.
        asyncio.run(run_workspace_evaluation(
            triggered_by="manual",
            evaluator_kind="nonsense",
        ))


# ----------------------------------------------------------------------
# Dispatch test (no RAG call, but exercises the import path)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_evaluator_kind_real_rag_v1_dispatches_correctly(conn):
    """Pass evaluator_kind='real_rag_v1' with an empty question_set
    filter — no questions to evaluate, so deps aren't built.
    Verifies the dispatch path resolves without import errors."""
    from app.services.eval.workspace_evaluator import run_workspace_evaluation

    # Doc-phase 179 — `core_chat` got 10 Wyoming uranium questions
    # seeded; use a still-empty set for the dispatch-only assertion.
    result = await run_workspace_evaluation(
        triggered_by="manual",
        question_set_filter="public_private_boundary",  # 0 active today
        evaluator_kind="real_rag_v1",
    )
    # Empty set → nothing to call; dispatch is exercised.
    assert result.question_count == 0
    assert result.success is True
