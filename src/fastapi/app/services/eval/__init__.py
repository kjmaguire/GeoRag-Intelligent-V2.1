"""Eval service — §10.2 + §10.6 — doc-phase 99 skeletons.

Three pieces:
- `seeds` — 8 question_set seed slots ready for §10.2 SME population
- `thresholds` — regression-threshold config + promotion-gate enforcer
- `runner` (future) — per-question execution harness wired by §10.4

Doc-phase 99 lands the seed slot scaffolding + threshold-gate
skeleton. §10.2 SME population pass and §10.4 runner graduation
land later.
"""
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

__all__ = [
    "QUESTION_SET_SLOTS",
    "QuestionSet",
    "seed_question_sets",
    "DEFAULT_REGRESSION_THRESHOLDS",
    "RegressionThresholds",
    "check_promotion_gate",
]
