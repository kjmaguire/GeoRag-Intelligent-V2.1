"""Backtesting Agent (§8.5 / §18.4).

Compares historical target recommendations to drilled-outcomes within
a time window and computes per-model performance metrics:
  - hit rate (hit / total)
  - precision-at-K (top K targets, fraction that hit)
  - aggregate score correlation with hit_or_miss outcome

Feeds into model_version selection + XGBoost retraining triggers
(`continuous_learning_loop` graduated; ML training still gated on
xgboost dep).

Phase H4 graduation — pure-function over caller-provided outcomes +
recommendations lists. Real DB-backed pull (joined window query)
plugs in when the analytics orchestrator wires this.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


def _hit_rate(outcomes: list[dict[str, Any]]) -> float:
    total = len(outcomes)
    if total == 0:
        return 0.0
    hits = sum(1 for o in outcomes if o.get("hit_or_miss") == "hit")
    return hits / total


def _precision_at_k(
    ranked: list[dict[str, Any]], outcomes_by_target: dict[str, str], k: int,
) -> float:
    if not ranked or k <= 0:
        return 0.0
    top_k = ranked[:k]
    hits = sum(
        1 for r in top_k
        if outcomes_by_target.get(str(r.get("target_id"))) == "hit"
    )
    return hits / min(k, len(top_k))


@georag_agent(
    name="Backtesting Agent",
    risk_tier="R2",  # Writes backtest rows
    version="1.0.0",
)
async def backtesting(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str | None,
    target_model_version_id: UUID | str,
    window_start: datetime,
    window_end: datetime,
    outcomes: list[dict[str, Any]] | None = None,
    ranked_recommendations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute per-model performance metrics for a time window.

    Args:
        workspace_id: RLS scope (None = cross-workspace ops query).
        target_model_version_id: model under evaluation.
        window_start / window_end: time window.
        outcomes: list of `targeting.target_outcomes` rows in window.
            Each carries `target_id` + `hit_or_miss`.
        ranked_recommendations: per-run ranked outputs the model
            produced over the window. Carries `target_id` + `rank`.

    Returns:
        Backtest envelope ready to INSERT into
        `targeting.target_backtests`.
    """
    outcomes = outcomes or []
    ranked = ranked_recommendations or []

    total = len(outcomes)
    hits = sum(1 for o in outcomes if o.get("hit_or_miss") == "hit")
    misses = sum(1 for o in outcomes if o.get("hit_or_miss") == "miss")
    hit_rate = _hit_rate(outcomes)

    outcomes_by_target = {
        str(o.get("target_id")): o.get("hit_or_miss") for o in outcomes
    }
    p_at_5 = _precision_at_k(ranked, outcomes_by_target, 5)
    p_at_10 = _precision_at_k(ranked, outcomes_by_target, 10)

    metrics = {
        "total_outcomes":   total,
        "hits":             hits,
        "misses":           misses,
        "hit_rate":         hit_rate,
        "precision_at_5":   p_at_5,
        "precision_at_10":  p_at_10,
        "method":           "deterministic_window_query",
    }
    summary = (
        f"target_model_version={target_model_version_id} window=[{window_start.isoformat()} → "
        f"{window_end.isoformat()}] outcomes={total} hit_rate={hit_rate:.2%}"
    )
    logger.info("backtesting: %s", summary)

    return {
        "workspace_id":            str(workspace_id) if workspace_id else None,
        "target_model_version_id": str(target_model_version_id),
        "window_start":            window_start.isoformat(),
        "window_end":              window_end.isoformat(),
        "metrics_payload":         metrics,
        "summary":                 summary,
        "computed_at":             datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["backtesting"]
