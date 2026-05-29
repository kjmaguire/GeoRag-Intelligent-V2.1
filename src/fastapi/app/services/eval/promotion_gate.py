"""§10.6 — promotion gate enforcer.

Compares a candidate eval run against a baseline and decides whether
the candidate may be promoted. The gate is *per-question_set
pass-rate regression*: if any set's pass rate drops by more than
``REGRESSION_THRESHOLD_PCT`` percentage points relative to baseline,
promotion is blocked.

This sits one level *above* ``thresholds.check_promotion_gate``:

- ``thresholds.check_promotion_gate`` operates on a precomputed
  ``run_summary`` dict (used inside ``evaluate_workspace`` while a
  run is still in-flight). It checks absolute fail counts +
  regression *counts*.

- ``promotion_gate.assess_promotion`` operates on two persisted
  ``eval.run_results`` rows in the database and computes the per-set
  pass-rate *delta*. This is the gate the operator hits from the
  admin cockpit when comparing two completed runs head-to-head.

The decision is recorded in the audit ledger as either
``eval.promotion.allowed`` or ``eval.promotion.blocked``.

Locked default (per §10 kickoff): a >5 percentage-point regression
in any single question_set blocks promotion. Override-with-rationale
path documented in RUNBOOK.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from app.audit import emit_audit

logger = logging.getLogger(__name__)


# ── Locked defaults (§10 kickoff) ────────────────────────────────────
REGRESSION_THRESHOLD_PCT: float = 5.0
"""Percentage-point drop in per-set pass-rate that blocks promotion."""

MIN_QUESTIONS_PER_SET_FOR_GATE: int = 3
"""Below this count, the per-set delta is too noisy to gate on. The
set is reported but does not contribute to a block decision."""


@dataclass(frozen=True, slots=True)
class SetDelta:
    """Per-question_set pass-rate delta between baseline and candidate."""

    question_set: str
    baseline_count: int
    candidate_count: int
    baseline_pass_pct: float
    candidate_pass_pct: float
    delta_pct: float  # candidate - baseline; negative = regression

    @property
    def is_regression(self) -> bool:
        return self.delta_pct < 0

    @property
    def is_blocking(self) -> bool:
        """Counts toward block: regression > threshold AND enough samples."""
        return (
            self.delta_pct < -REGRESSION_THRESHOLD_PCT
            and self.baseline_count >= MIN_QUESTIONS_PER_SET_FOR_GATE
            and self.candidate_count >= MIN_QUESTIONS_PER_SET_FOR_GATE
        )


@dataclass(frozen=True, slots=True)
class PromotionAssessment:
    """Outcome of ``assess_promotion``."""

    allow: bool
    workspace_id: UUID
    candidate_run_id: UUID
    baseline_run_id: UUID
    set_deltas: list[SetDelta]
    blocking_sets: list[str]
    regressions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow": self.allow,
            "workspace_id": str(self.workspace_id),
            "candidate_run_id": str(self.candidate_run_id),
            "baseline_run_id": str(self.baseline_run_id),
            "regression_threshold_pct": REGRESSION_THRESHOLD_PCT,
            "blocking_sets": list(self.blocking_sets),
            "set_deltas": [
                {
                    "question_set": d.question_set,
                    "baseline_count": d.baseline_count,
                    "candidate_count": d.candidate_count,
                    "baseline_pass_pct": round(d.baseline_pass_pct, 2),
                    "candidate_pass_pct": round(d.candidate_pass_pct, 2),
                    "delta_pct": round(d.delta_pct, 2),
                    "is_blocking": d.is_blocking,
                }
                for d in self.set_deltas
            ],
            "regressions": list(self.regressions),
        }


async def _fetch_run_pass_rates_per_set(
    conn: asyncpg.Connection,
    run_id: UUID,
) -> dict[str, tuple[int, int]]:
    """Return ``{question_set: (pass_count, total_count)}`` for a run.

    Joins ``run_results`` to ``golden_questions`` to surface the set
    each result belongs to.
    """
    rows = await conn.fetch(
        """
        SELECT gq.question_set,
               sum(CASE WHEN rr.passed THEN 1 ELSE 0 END)::int AS pass_count,
               count(*)::int AS total_count
          FROM eval.run_results rr
          JOIN eval.golden_questions gq ON gq.question_id = rr.question_id
         WHERE rr.run_id = $1
         GROUP BY gq.question_set
        """,
        run_id,
    )
    return {r["question_set"]: (r["pass_count"], r["total_count"]) for r in rows}


async def _list_per_question_regressions(
    conn: asyncpg.Connection,
    candidate_run_id: UUID,
    baseline_run_id: UUID,
) -> list[dict[str, Any]]:
    """List the questions that regressed (passed in baseline, failed in candidate)."""
    rows = await conn.fetch(
        """
        SELECT gq.question_id::text AS question_id,
               gq.question_set,
               br.passed             AS baseline_pass,
               cr.passed             AS candidate_pass
          FROM eval.run_results br
          JOIN eval.run_results cr
            ON cr.question_id = br.question_id
           AND cr.run_id = $1
          JOIN eval.golden_questions gq
            ON gq.question_id = br.question_id
         WHERE br.run_id = $2
           AND br.passed = TRUE
           AND cr.passed = FALSE
         ORDER BY gq.question_set, gq.question_id
        """,
        candidate_run_id,
        baseline_run_id,
    )
    return [
        {
            "question_id": r["question_id"],
            "question_set": r["question_set"],
            "baseline_pass": bool(r["baseline_pass"]),
            "candidate_pass": bool(r["candidate_pass"]),
        }
        for r in rows
    ]


async def assess_promotion(
    pool: asyncpg.Pool,
    *,
    workspace_id: UUID,
    candidate_run_id: UUID,
    baseline_run_id: UUID,
    actor_user_id: int | None = None,
    emit_audit_row: bool = True,
) -> PromotionAssessment:
    """Compare a candidate eval run against a baseline.

    Returns ``PromotionAssessment.allow=False`` if any question_set
    regressed by more than ``REGRESSION_THRESHOLD_PCT`` percentage
    points (default 5 pp, locked in §10 kickoff). Per-set deltas with
    fewer than ``MIN_QUESTIONS_PER_SET_FOR_GATE`` samples on either
    side are still surfaced but do not contribute to the block
    decision (too noisy).

    Emits ``eval.promotion.allowed`` or ``eval.promotion.blocked``
    audit rows by default. Pass ``emit_audit_row=False`` for dry
    runs.
    """
    if candidate_run_id == baseline_run_id:
        raise ValueError("candidate_run_id and baseline_run_id must differ")

    async with pool.acquire() as conn:
        candidate = await _fetch_run_pass_rates_per_set(conn, candidate_run_id)
        baseline = await _fetch_run_pass_rates_per_set(conn, baseline_run_id)
        regressions = await _list_per_question_regressions(
            conn, candidate_run_id, baseline_run_id
        )

    all_sets = sorted(set(candidate) | set(baseline))
    set_deltas: list[SetDelta] = []
    blocking_sets: list[str] = []

    for qset in all_sets:
        b_pass, b_total = baseline.get(qset, (0, 0))
        c_pass, c_total = candidate.get(qset, (0, 0))
        b_pct = (b_pass / b_total * 100.0) if b_total > 0 else 0.0
        c_pct = (c_pass / c_total * 100.0) if c_total > 0 else 0.0
        delta = c_pct - b_pct
        sd = SetDelta(
            question_set=qset,
            baseline_count=b_total,
            candidate_count=c_total,
            baseline_pass_pct=b_pct,
            candidate_pass_pct=c_pct,
            delta_pct=delta,
        )
        set_deltas.append(sd)
        if sd.is_blocking:
            blocking_sets.append(qset)

    allow = len(blocking_sets) == 0
    assessment = PromotionAssessment(
        allow=allow,
        workspace_id=workspace_id,
        candidate_run_id=candidate_run_id,
        baseline_run_id=baseline_run_id,
        set_deltas=set_deltas,
        blocking_sets=blocking_sets,
        regressions=regressions,
    )

    if emit_audit_row:
        action_type = (
            "eval.promotion.allowed" if allow else "eval.promotion.blocked"
        )
        try:
            await emit_audit(
                pool,
                action_type=action_type,
                workspace_id=workspace_id,
                actor_id=actor_user_id,
                actor_kind="user" if actor_user_id else "system",
                target_schema="eval",
                target_table="run_results",
                target_id=str(candidate_run_id),
                payload=assessment.to_dict(),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "promotion_gate: audit emission failed "
                "(workspace_id=%s candidate=%s baseline=%s)",
                workspace_id,
                candidate_run_id,
                baseline_run_id,
            )

    return assessment


__all__ = [
    "REGRESSION_THRESHOLD_PCT",
    "MIN_QUESTIONS_PER_SET_FOR_GATE",
    "SetDelta",
    "PromotionAssessment",
    "assess_promotion",
]
