"""Regression-threshold config + promotion gate (§10.6) — doc-phase 99.

Per master plan §24.4, eval results gate prompt-change / model-change
promotion: if a candidate run regresses beyond thresholds vs the
prior baseline, promotion is blocked (operator can override with
logged rationale).

Threshold defaults are conservative; Kyle's recommended starting
state is **warning-only for 2 weeks**, then flip to blocking (per
§10 scope proposal open question #2).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RegressionThresholds(BaseModel):
    """Per-question-set + global thresholds."""

    # Global guardrails
    max_absolute_fail_count: int = Field(
        default=5,
        description="If more than this many questions fail outright, "
                    "promotion blocks regardless of regression delta.",
    )
    max_regression_count: int = Field(
        default=2,
        description="If more than this many questions regressed from "
                    "passing→failing vs baseline, promotion blocks.",
    )

    # Per-set tolerances (regression_count thresholds by set name)
    per_set_max_regression: dict[str, int] = Field(
        default_factory=lambda: {
            "core_chat": 1,
            "public_private_boundary": 0,    # §2.9 is a regulatory anchor; zero tolerance
            "numeric_grounding": 1,
            "refusal_correctness": 1,
            "target_recommendation": 0,      # R5 sign-off; zero tolerance
            "report_section": 2,
            "schema_mapping": 2,
            "ocr_triage": 3,
        }
    )

    # Mode
    mode: str = Field(
        default="warning_only",
        description="warning_only | blocking. Per §10.6, start in "
                    "warning_only for 2 weeks then flip to blocking.",
    )


DEFAULT_REGRESSION_THRESHOLDS = RegressionThresholds()


async def check_promotion_gate(
    run_summary: dict[str, Any],
    *,
    thresholds: RegressionThresholds | None = None,
) -> dict[str, Any]:
    """Decide whether the run blocks promotion. Graduated doc-phase 132.

    Three gates evaluated against the supplied thresholds:

      1. ``max_absolute_fail_count`` — total ``fail_count`` exceeds it
      2. ``max_regression_count`` — total ``regression_count`` exceeds it
      3. ``per_set_max_regression`` — any question_set's regression
         count exceeds its set-specific cap

    Args:
        run_summary: dict with at minimum ``pass_count``, ``fail_count``,
            ``regression_count``. May also include ``per_set`` ->
            ``{set_name: {pass, fail, regression}}`` for the per-set
            check.
        thresholds: override default thresholds.

    Returns:
        ``{
            "blocks_promotion": bool,
            "reasons": [str],
            "mode": "warning_only" | "blocking"
        }``

    Mode semantics:
      - ``blocking``: ``blocks_promotion`` set to True if any gate
        triggers. ``reasons`` enumerates which gate(s) hit.
      - ``warning_only``: ``blocks_promotion`` always False even if
        gates would trigger; ``reasons`` still populated so callers
        can log/surface warnings.
    """
    t = thresholds or DEFAULT_REGRESSION_THRESHOLDS
    fail_count = int(run_summary.get("fail_count", 0))
    regression_count = int(run_summary.get("regression_count", 0))
    per_set = run_summary.get("per_set") or {}

    reasons: list[str] = []

    if fail_count > t.max_absolute_fail_count:
        reasons.append(
            f"absolute_fail_count={fail_count} exceeds threshold "
            f"max_absolute_fail_count={t.max_absolute_fail_count}"
        )

    if regression_count > t.max_regression_count:
        reasons.append(
            f"regression_count={regression_count} exceeds threshold "
            f"max_regression_count={t.max_regression_count}"
        )

    for set_name, set_caps in t.per_set_max_regression.items():
        set_bucket = per_set.get(set_name) or {}
        set_regressions = int(set_bucket.get("regression", 0))
        if set_regressions > set_caps:
            reasons.append(
                f"per_set[{set_name}].regression={set_regressions} "
                f"exceeds threshold per_set_max_regression={set_caps}"
            )

    would_block = len(reasons) > 0
    blocks_promotion = (t.mode == "blocking") and would_block

    return {
        "blocks_promotion": blocks_promotion,
        "would_block": would_block,
        "reasons": reasons,
        "mode": t.mode,
    }


__all__ = [
    "RegressionThresholds",
    "DEFAULT_REGRESSION_THRESHOLDS",
    "check_promotion_gate",
]
