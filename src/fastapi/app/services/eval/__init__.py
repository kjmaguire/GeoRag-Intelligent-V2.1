"""Eval service — §10.2 + §10.4 + §10.6 — evaluators restored and live.

Five pieces:
- `seeds` — 8 question_set seed slots ready for §10.2 SME population
- `thresholds` — regression-threshold config + promotion-gate enforcer
- `workspace_evaluator` — per-question fanout + result aggregation
  (§10.4 doc-phase 132; restored 2026-08-14 from the runtime-eval trim)
- `real_rag_evaluator` — real retrieval-augmented evaluator that wires
  `AgentDeps` into `run_deterministic_rag` (§10.4 doc-phase 162;
  restored 2026-08-14 alongside workspace_evaluator)
- `run_workspace_evaluation` (workspace_evaluator) / `run_golden_benchmark.py`
  (script) are the live entry points into the runner

`scripts/run_golden_benchmark.py` imports directly from the submodules
rather than this package, so these re-exports are a consistency
convenience, not the only import path.
"""
from app.services.eval.real_rag_evaluator import (
    evaluate_question_real_rag,
)
from app.services.eval.seeds import (
    QUESTION_SET_SLOTS,
    QuestionSet,
    seed_question_sets,
)
from app.services.eval.thresholds import (
    DEFAULT_REGRESSION_THRESHOLDS,
    RegressionThresholds,
    check_promotion_gate,
)
from app.services.eval.workspace_evaluator import (
    QuestionRecord,
    QuestionResult,
    WorkspaceEvaluationResult,
    evaluate_question,
    run_workspace_evaluation,
)

__all__ = [
    "QUESTION_SET_SLOTS",
    "QuestionSet",
    "seed_question_sets",
    "DEFAULT_REGRESSION_THRESHOLDS",
    "RegressionThresholds",
    "check_promotion_gate",
    "QuestionRecord",
    "QuestionResult",
    "WorkspaceEvaluationResult",
    "evaluate_question",
    "run_workspace_evaluation",
    "evaluate_question_real_rag",
]
