"""XGBoost target scoring + SHAP (§12.4 + §12.5) — doc-phase 101 skeletons.

Two sibling utility modules:
- `xgboost_inference` — alternate scoring branch in the §8.7 weighted
  scoring formula. Loads a serialized XGBoost model from
  `target_model_versions.constraint_payload`, runs inference per
  zone, returns aggregate_score.
- `shap_writer` — replaces deterministic per-factor decomposition
  with SHAP attributions when scoring_kind='xgboost'. Writes one
  `target_score_factors` row per SHAP feature contribution per zone.

Both wait on §12.1 image-rebuild (xgboost + shap + scikit-learn).

Per §18.3 SHAP integration is MANDATORY for XGBoost scores — every
target score must carry per-factor contributions for the "no
black-box targeting" rule. The weighted-path's deterministic
decomposition + the xgboost-path's SHAP attributions both write to
the same `target_score_factors` schema so downstream consumers
(Report Builder's per-target briefs) don't branch on scoring_kind.
"""
from app.services.target_scoring_ml.ab_comparison import (
    ABComparisonResult,
    Strategy,
    choose_display_strategy,
    compute_ab_scores,
)
from app.services.target_scoring_ml.xgboost_inference import (
    XGBoostInferenceResult,
    score_zone_xgboost,
)
from app.services.target_scoring_ml.shap_writer import (
    write_shap_factors,
)

__all__ = [
    "ABComparisonResult",
    "Strategy",
    "XGBoostInferenceResult",
    "choose_display_strategy",
    "compute_ab_scores",
    "score_zone_xgboost",
    "write_shap_factors",
]
