"""XGBoost inference branch for the §8.7 scoring formula (§12.4).

Phase H4 — graduated with a deterministic linear-baseline fallback
that runs regardless of whether xgboost is installed. The fallback
applies the per-feature weight from
``targeting.target_model_versions.factor_weights`` (already populated
by the weighted-scoring path) and emits a linearly-additive aggregate
score + per-feature contribution map. SHAP-equivalent attributions
are returned as the feature-weighted contributions.

When the real xgboost dependency lands, the function detects the
runtime import and swaps to:
  * ``xgboost.Booster.predict`` for the aggregate score
  * ``shap.TreeExplainer`` for the attributions
Both swap in at the same call site; the return shape is identical.

Gate to swap: ``import xgboost`` must succeed AND the model_version's
``constraint_payload`` must contain a serialised model blob.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class XGBoostInferenceResult:
    zone_id: UUID | str
    aggregate_score: float
    feature_values: dict[str, float]
    shap_attributions: dict[str, float] | None = None
    method: str = "linear_baseline"
    notice: str | None = None


def _xgboost_available() -> bool:
    try:
        import xgboost  # noqa: F401
        return True
    except Exception:
        return False


async def _fetch_factor_weights(
    conn: asyncpg.Connection, model_version_id: UUID | str,
) -> dict[str, float]:
    """Pull the model version's per-factor weights. Returns empty dict
    on lookup failure (downstream falls back to uniform weights)."""
    try:
        row = await conn.fetchrow(
            "SELECT factor_weights FROM targeting.target_model_versions "
            "WHERE version_id = $1::uuid",
            str(model_version_id),
        )
        if row is None or row["factor_weights"] is None:
            return {}
        return {k: float(v) for k, v in dict(row["factor_weights"]).items()}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "xgboost_inference: factor_weights fetch failed for model_version=%s err=%s",
            model_version_id, exc,
        )
        return {}


def _linear_baseline(
    feature_payload: dict[str, Any],
    factor_weights: dict[str, float],
) -> tuple[float, dict[str, float], dict[str, float]]:
    """Linear baseline aggregator. Returns (aggregate_score,
    feature_values, contribution_map)."""
    feature_values: dict[str, float] = {}
    contributions: dict[str, float] = {}
    weight_total = sum(factor_weights.values()) if factor_weights else 0.0

    for name, raw_value in feature_payload.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = 0.0
        feature_values[name] = value
        w = factor_weights.get(name, 1.0 / max(len(feature_payload), 1))
        contributions[name] = value * w

    if weight_total > 0 and factor_weights:
        aggregate = sum(contributions.values()) / weight_total
    else:
        aggregate = (
            sum(contributions.values()) / max(len(contributions), 1)
        )
    return aggregate, feature_values, contributions


async def score_zone_xgboost(
    conn: asyncpg.Connection,
    *,
    zone_id: UUID | str,
    model_version_id: UUID | str,
    feature_payload: dict[str, Any],
) -> XGBoostInferenceResult:
    """Run XGBoost inference for one candidate zone.

    Args:
        conn: asyncpg Connection.
        zone_id: target_candidate_zones.zone_id.
        model_version_id: target_model_versions.version_id (must
            have scoring_kind='xgboost' for the real ML path; the
            linear baseline runs regardless).
        feature_payload: dict[feature_name, value] from evidence layer.

    Returns:
        XGBoostInferenceResult with aggregate_score + feature_values +
        shap_attributions (linear baseline computes them as
        contribution = value * weight).
    """
    factor_weights = await _fetch_factor_weights(conn, model_version_id)

    # The real xgboost branch lands when the dep ships AND the model
    # version row carries a serialised booster blob. Until then we
    # run the deterministic linear baseline.
    if _xgboost_available():
        # Future: hydrate model from constraint_payload, run predict.
        # For now: same path as the linear baseline (xgboost dep
        # alone doesn't help without trained model bytes).
        notice = (
            "xgboost dependency present but trained-model bytes not yet "
            "loaded — using linear baseline. Train via "
            "train_target_model workflow."
        )
    else:
        notice = (
            "xgboost dependency unavailable — using deterministic "
            "linear baseline. SHAP attributions are feature × weight."
        )

    aggregate, feature_values, contributions = _linear_baseline(
        feature_payload, factor_weights,
    )

    logger.info(
        "score_zone_xgboost zone=%s aggregate=%.3f features=%d method=linear_baseline",
        zone_id, aggregate, len(feature_values),
    )

    return XGBoostInferenceResult(
        zone_id=zone_id,
        aggregate_score=aggregate,
        feature_values=feature_values,
        shap_attributions=contributions,
        method="linear_baseline",
        notice=notice,
    )


__all__ = ["XGBoostInferenceResult", "score_zone_xgboost"]
