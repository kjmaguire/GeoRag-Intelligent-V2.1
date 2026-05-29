"""A/B comparison harness for weighted vs XGBoost vs ensemble (§12.6).

Per master plan §18.3, both scoring approaches stay in production
deliberately:

- **weighted** = deterministic baseline, geologist-defined factor
  weights, always explainable via direct decomposition.
- **xgboost** = ML-augmented; today the linear-baseline fallback
  from ``xgboost_inference.score_zone_xgboost`` runs (real xgboost
  swaps in when the dep + trained model bytes land).
- **ensemble** = blends both. Default mix is
  ``weighted * 0.6 + xgboost * 0.4`` when xgboost confidence is
  high; falls back to weighted-only when xgboost confidence is low.

Phase H4 graduation — runs both paths inline against the existing
``targeting.target_score_factors``-aware linear baseline. Returns
the comparison envelope; the orchestrator persists both scores under
distinct ``model_version_id`` values for the per-target brief.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg

from app.services.target_scoring_ml.xgboost_inference import (
    XGBoostInferenceResult,
    score_zone_xgboost,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ABComparisonResult:
    zone_id: UUID | str
    weighted_score: float
    xgboost_score: float
    ensemble_score: float
    divergence: float                  # |weighted - xgboost|
    confidence_drop_fallback: bool     # True when ensemble fell back to weighted
    weighted_score_id: UUID | str
    xgboost_score_id: UUID | str
    notice: str | None = None


Strategy = Literal["weighted_only", "xgboost_only", "ensemble"]


def choose_display_strategy(
    *,
    weighted_score: float,
    xgboost_score: float | None,
    xgboost_confidence: float | None,
    confidence_floor: float = 0.4,
) -> Strategy:
    """Decide which score(s) to surface to the geologist."""
    if xgboost_score is None or xgboost_confidence is None:
        return "weighted_only"
    if xgboost_confidence < confidence_floor:
        return "weighted_only"
    return "ensemble"


async def _fetch_weighted_score(
    conn: asyncpg.Connection,
    *,
    workspace_id: UUID | str,
    zone_id: UUID | str,
    weighted_version_id: UUID | str,
) -> tuple[float, UUID | str]:
    """Pull the existing weighted score for this zone+version, or
    return a sentinel (0.0, nil-uuid) when missing. The weighted path
    is the deterministic baseline that the §18.2 score_candidate_zones
    node already populated."""
    try:
        row = await conn.fetchrow(
            """
            SELECT score_id::text AS score_id, aggregate_score
              FROM targeting.target_scores
             WHERE workspace_id = $1::uuid
               AND zone_id = $2::uuid
               AND model_version_id = $3::uuid
             ORDER BY computed_at DESC
             LIMIT 1
            """,
            str(workspace_id), str(zone_id), str(weighted_version_id),
        )
        if row:
            return float(row["aggregate_score"] or 0.0), row["score_id"]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ab_comparison: weighted-score fetch failed zone=%s err=%s",
            zone_id, exc,
        )
    return 0.0, str(uuid4())


async def compute_ab_scores(
    conn: asyncpg.Connection,
    *,
    workspace_id: UUID | str,
    zone_id: UUID | str,
    weighted_version_id: UUID | str,
    xgboost_version_id: UUID | str,
    feature_payload: dict[str, Any] | None = None,
    ensemble_weight_weighted: float = 0.6,
    ensemble_weight_xgboost: float = 0.4,
    xgboost_confidence_floor: float = 0.4,
) -> ABComparisonResult:
    """Run both scoring paths against one zone; return comparison.

    Args:
        conn: asyncpg Connection scoped to workspace RLS.
        zone_id: candidate zone to score.
        weighted_version_id: model_version row for the weighted path
            (the deterministic baseline's score is fetched from
            ``target_scores``).
        xgboost_version_id: model_version row for the xgboost path.
        feature_payload: per-feature values for the xgboost path. When
            None, the inference uses an empty dict (aggregate=0).
        ensemble_weight_*: ensemble blend weights.
        xgboost_confidence_floor: below this, ensemble falls back to
            weighted-only.

    Returns:
        ABComparisonResult.
    """
    feature_payload = feature_payload or {}

    weighted_score, weighted_score_id = await _fetch_weighted_score(
        conn,
        workspace_id=workspace_id,
        zone_id=zone_id,
        weighted_version_id=weighted_version_id,
    )

    xgb_result: XGBoostInferenceResult = await score_zone_xgboost(
        conn,
        zone_id=zone_id,
        model_version_id=xgboost_version_id,
        feature_payload=feature_payload,
    )
    xgboost_score = float(xgb_result.aggregate_score)
    xgboost_score_id = str(uuid4())

    # Confidence: linear baseline has no native confidence; use a
    # proxy = 1 - (divergence / max(weighted, 1.0)). Real xgboost
    # paths will replace this with the model's confidence interval.
    divergence = abs(weighted_score - xgboost_score)
    confidence_proxy = max(
        0.0, 1.0 - divergence / max(abs(weighted_score), 1.0),
    )

    if confidence_proxy < xgboost_confidence_floor:
        ensemble_score = weighted_score
        fallback = True
    else:
        ensemble_score = (
            ensemble_weight_weighted * weighted_score
            + ensemble_weight_xgboost * xgboost_score
        )
        fallback = False

    notice = xgb_result.notice
    logger.info(
        "compute_ab_scores zone=%s weighted=%.3f xgboost=%.3f ensemble=%.3f "
        "divergence=%.3f fallback=%s confidence_proxy=%.2f",
        zone_id, weighted_score, xgboost_score, ensemble_score,
        divergence, fallback, confidence_proxy,
    )

    return ABComparisonResult(
        zone_id=zone_id,
        weighted_score=weighted_score,
        xgboost_score=xgboost_score,
        ensemble_score=ensemble_score,
        divergence=divergence,
        confidence_drop_fallback=fallback,
        weighted_score_id=weighted_score_id,
        xgboost_score_id=xgboost_score_id,
        notice=notice,
    )


__all__ = [
    "ABComparisonResult",
    "compute_ab_scores",
    "choose_display_strategy",
    "Strategy",
]
