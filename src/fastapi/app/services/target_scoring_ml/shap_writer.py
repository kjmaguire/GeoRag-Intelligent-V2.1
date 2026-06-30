"""SHAP attribution writer (§12.5).

Per master plan §18.3, SHAP explanations are MANDATORY for every
XGBoost target score. This module writes one
``targeting.target_score_factors`` row per SHAP feature contribution
per scored zone.

Schema-compatible with the weighted-scoring path: both write to the
same ``target_score_factors`` table, so downstream consumers
(per-target briefs in the Target Recommendation Report) don't branch
on scoring_kind.

Phase H4 graduation — the writer now performs the real INSERT against
``targeting.target_score_factors`` using caller-supplied attributions.
The attribution values come from either real SHAP (when xgboost +
shap deps ship) OR the linear baseline emitted by
from app.db import bind_workspace_scope
``xgboost_inference.score_zone_xgboost`` (always available). The
table schema doesn't care which produced the numbers.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


_INSERT_SQL = """
INSERT INTO targeting.target_score_factors (
    factor_id, score_id, factor_name, factor_value,
    factor_weight, contribution, evidence_chunk_ids, workspace_id
)
VALUES (
    gen_random_uuid(), $1::uuid, $2, $3, $4, $5, $6::text[], $7::uuid
)
"""


async def write_shap_factors(
    conn: asyncpg.Connection,
    *,
    score_id: UUID | str,
    shap_attributions: Mapping[str, float],
    feature_values: Mapping[str, float],
    evidence_chunk_id_lookup: Mapping[str, list[str]] | None = None,
    workspace_id: UUID | str | None = None,
) -> int:
    """Insert per-feature SHAP rows into target_score_factors.

    Args:
        conn: asyncpg Connection.
        score_id: parent target_scores.score_id.
        shap_attributions: per-feature SHAP contribution (signed).
            For the linear baseline path, this equals
            ``value * weight``; for real SHAP, the model's
            TreeExplainer output.
        feature_values: per-feature raw input value.
        evidence_chunk_id_lookup: optional per-feature list of chunk
            ids that backed the feature value.
        workspace_id: optional explicit workspace_id (set on rows for
            Block-3 RLS). Required when the caller's session doesn't
            already have ``app.workspace_id`` GUC set.

    Returns:
        rows_written count.
    """
    if not shap_attributions:
        return 0

    # Resolve workspace_id from the score row when caller didn't pass it.
    if workspace_id is None:
        try:
            row = await conn.fetchrow(
                "SELECT workspace_id::text FROM targeting.target_scores "
                "WHERE score_id = $1::uuid",
                str(score_id),
            )
            if row and row["workspace_id"]:
                workspace_id = row["workspace_id"]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "write_shap_factors: workspace_id lookup failed score=%s err=%s",
                score_id, exc,
            )

    if workspace_id is None:
        logger.error(
            "write_shap_factors: refusing to insert without workspace_id "
            "(Block-3 RLS requires it). score_id=%s", score_id,
        )
        return 0

    # Make sure the session has the GUC set so the WITH CHECK passes.
    await bind_workspace_scope(
        conn, workspace_id=str(workspace_id), site="shap_writer",
    )

    rows_written = 0
    # The contribution from a SHAP attribution IS the attribution
    # itself. factor_weight is the underlying weight (or 1.0 when we
    # don't have one); factor_value is the raw input.
    for feature_name, attribution in shap_attributions.items():
        value = float(feature_values.get(feature_name, 0.0))
        contribution = float(attribution)
        # factor_weight: best-effort inferred as contribution / value
        # (linear baseline guarantees this is correct; real SHAP it's
        # an approximation since the attribution is non-linear).
        weight = (
            contribution / value if value not in (0.0, None) else 1.0
        )
        chunk_ids = list((evidence_chunk_id_lookup or {}).get(feature_name, []))

        try:
            await conn.execute(
                _INSERT_SQL,
                str(score_id),
                feature_name,
                value,
                weight,
                contribution,
                chunk_ids,
                str(workspace_id),
            )
            rows_written += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "write_shap_factors: INSERT failed factor=%s err=%s",
                feature_name, exc,
            )

    logger.info(
        "write_shap_factors: score_id=%s rows_written=%d", score_id, rows_written,
    )
    return rows_written


__all__ = ["write_shap_factors"]
