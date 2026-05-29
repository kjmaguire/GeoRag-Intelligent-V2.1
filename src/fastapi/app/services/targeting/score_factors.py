"""SHAP-equivalent per-factor scoring for target candidate zones.

Phase G.1 (master-plan §8 + §18.2) — scores a candidate zone against a
deposit model's active version, producing one ``targeting.target_scores``
row plus one ``targeting.target_score_factors`` row per factor.

The scoring is **weighted-additive**, not XGBoost. Phase 12 will swap in
the XGBoost path once enough field outcomes accumulate; until then the
weighted path is the production scorer + the SHAP-equivalent
per-factor breakdown is the explanation surface the master plan §8
"Done when" line requires.

Inputs
------
* ``zone_evidence: dict[str, float]`` — observed factor strengths in
  [0, 1] for the zone. The classifier / signal collectors upstream
  produce these. Missing factors are treated as 0 (no evidence).
* ``model_version_row: dict`` — from ``targeting.target_model_versions``
  carrying the ``factor_weights`` JSONB.

Output
------
A ``ScoredZone`` dataclass with:
* ``aggregate_score`` — sum of (factor_value × factor_weight) normalised
  to [-1, 1] by Σ|weight|.
* ``factors`` — list of ``ScoreFactor`` with per-factor value, weight,
  signed contribution, and a short rationale string for the explanation
  surface.

Why this is the SHAP-equivalent
-------------------------------
With a weighted-additive scorer, the per-factor contribution to the
aggregate IS its SHAP value — there's no nonlinearity for Shapley
attribution to disentangle. ``contribution_i = value_i × weight_i``;
``aggregate = Σ contribution_i / Σ|weight_i|``. Phase 12 will swap to a
true XGBoost + shap.Explainer pipeline once enough outcomes exist;
until then this is the explanation surface the master plan calls for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class ScoreFactor:
    """A single per-factor contribution to a zone's aggregate score."""

    factor_name: str
    factor_value: float          # observed strength in [0, 1]
    factor_weight: float          # from factor_weights JSONB (can be negative)
    contribution: float           # = factor_value × factor_weight
    evidence_chunk_ids: list[str] = field(default_factory=list)
    rationale: str = ""           # short human-readable explanation


@dataclass
class ScoredZone:
    """Output of ``score_candidate_zone`` — ready for INSERT into
    ``targeting.target_scores`` + ``targeting.target_score_factors``.
    """

    aggregate_score: float        # ∈ [-1, 1] after normalisation
    factors: list[ScoreFactor]
    model_version_id: UUID
    zone_id: UUID | None = None
    workspace_id: UUID | None = None


def score_candidate_zone(
    *,
    zone_evidence: dict[str, float],
    factor_weights: dict[str, float],
    model_version_id: UUID,
    zone_id: UUID | None = None,
    workspace_id: UUID | None = None,
    evidence_chunk_map: dict[str, list[str]] | None = None,
) -> ScoredZone:
    """Compute the weighted-additive score for one candidate zone.

    The factor_weights dict comes verbatim from
    ``target_model_versions.factor_weights`` — keys are the canonical
    factor names from the deposit-model template, values are signed
    weights (positive for indicators that boost score, negative for
    indicators that drop it).

    Args:
        zone_evidence: ``{factor_name: observed_strength_in_0_1}``.
            Factors not in the dict get observed_strength=0 (no
            evidence found / no signal collector wired). Strength is
            domain-specific: e.g. for ``graphite_conductor_present`` it's
            the EM conductance normalised to [0, 1]; for an alteration
            factor it's the alteration-pixel coverage ratio over the
            zone's footprint.
        factor_weights: the model version's flat name→weight map.
        model_version_id: UUID of the active model version. Stamped on
            every produced ScoreFactor so the score can be reproduced
            from the ledger.
        zone_id, workspace_id: pass-through for the INSERT layer.
        evidence_chunk_map: optional ``{factor_name: [chunk_id, ...]}``
            mapping back to Qdrant chunks / silver row IDs that support
            the observed strength. Stored on each ScoreFactor for the
            Trust Inspector drilldown.

    Returns:
        ScoredZone ready for persistence.
    """
    evidence_chunk_map = evidence_chunk_map or {}
    factors: list[ScoreFactor] = []
    contribution_sum = 0.0
    weight_abs_sum = 0.0

    for factor_name, weight in factor_weights.items():
        weight_f = float(weight)
        value = float(zone_evidence.get(factor_name, 0.0))
        # Clamp value into [0, 1] — protects the aggregate from runaway
        # signal collectors. A factor either contributes (0..1) or is
        # absent (0). Negative observed values aren't meaningful here;
        # the SIGN of the contribution comes from the weight, not the
        # value.
        if value < 0.0:
            value = 0.0
        elif value > 1.0:
            value = 1.0

        contribution = value * weight_f
        factors.append(
            ScoreFactor(
                factor_name=factor_name,
                factor_value=value,
                factor_weight=weight_f,
                contribution=contribution,
                evidence_chunk_ids=evidence_chunk_map.get(factor_name, []),
                rationale=_rationale_for(factor_name, value, weight_f),
            )
        )
        contribution_sum += contribution
        weight_abs_sum += abs(weight_f)

    # Normalise so aggregate is in [-1, 1] regardless of how many
    # factors a model defines. weight_abs_sum is 0 only for the
    # `custom` template's empty version — return 0 in that case.
    aggregate = (contribution_sum / weight_abs_sum) if weight_abs_sum > 0 else 0.0
    # Tiny float drift can push past 1.0; clamp.
    aggregate = max(-1.0, min(1.0, aggregate))

    logger.info(
        "score_candidate_zone: model_version=%s factors=%d "
        "contribution_sum=%.3f weight_abs_sum=%.3f aggregate=%.3f",
        model_version_id, len(factors),
        contribution_sum, weight_abs_sum, aggregate,
    )

    return ScoredZone(
        aggregate_score=aggregate,
        factors=factors,
        model_version_id=model_version_id,
        zone_id=zone_id,
        workspace_id=workspace_id,
    )


def _rationale_for(name: str, value: float, weight: float) -> str:
    """Build a short rationale string for the Trust Inspector drilldown."""
    sign = "boost" if weight >= 0 else "penalty"
    strength = "no signal" if value <= 0.05 else (
        "weak signal" if value < 0.4 else
        "moderate signal" if value < 0.75 else
        "strong signal"
    )
    contribution = value * weight
    return (
        f"{name}: {strength} (observed={value:.2f}) × weight={weight:+.2f} "
        f"({sign}) → contribution={contribution:+.3f}"
    )


async def persist_scored_zone(
    *,
    pg_pool: Any,
    scored: ScoredZone,
) -> UUID:
    """Persist a ScoredZone into ``targeting.target_scores`` +
    ``targeting.target_score_factors``. Returns the inserted score_id.

    Idempotent on the ``(zone_id, model_version_id)`` unique key —
    re-running with the same scored zone replaces the prior row's
    factors atomically (DELETE + INSERT inside a transaction).
    """
    if scored.zone_id is None or scored.workspace_id is None:
        raise ValueError("persist_scored_zone requires zone_id + workspace_id")

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            # Upsert score row.
            score_id = await conn.fetchval(
                """
                INSERT INTO targeting.target_scores (
                    zone_id, workspace_id, model_version_id,
                    aggregate_score, aggregate_uncertainty, computed_at
                )
                VALUES ($1, $2, $3, $4, NULL, NOW())
                ON CONFLICT (zone_id, model_version_id) DO UPDATE
                SET aggregate_score = EXCLUDED.aggregate_score,
                    computed_at = NOW()
                RETURNING score_id
                """,
                scored.zone_id, scored.workspace_id, scored.model_version_id,
                scored.aggregate_score,
            )

            # Wipe old factor rows + insert fresh ones.
            await conn.execute(
                "DELETE FROM targeting.target_score_factors WHERE score_id = $1",
                score_id,
            )
            if scored.factors:
                await conn.executemany(
                    """
                    INSERT INTO targeting.target_score_factors (
                        score_id, factor_name, factor_value,
                        factor_weight, contribution, evidence_chunk_ids
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    [
                        (
                            score_id,
                            f.factor_name,
                            f.factor_value,
                            f.factor_weight,
                            f.contribution,
                            f.evidence_chunk_ids,
                        )
                        for f in scored.factors
                    ],
                )

    return score_id


__all__ = [
    "ScoreFactor",
    "ScoredZone",
    "score_candidate_zone",
    "persist_scored_zone",
]
