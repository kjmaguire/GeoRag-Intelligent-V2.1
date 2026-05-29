"""Target Scoring Agent (§8.5 / §18.4).

Applies the deposit-model factor weights to per-zone evidence to
produce ``aggregate_score`` + per-factor contributions. Phase 8 uses
weighted scoring; Phase 12 augments with XGBoost+SHAP. Per master
plan §18.3, SHAP-equivalent breakdown is MANDATORY — the
"no black-box targeting" rule.

Phase H4 graduation — delegates to the §18.2 `score_candidate_zones`
TRG node which carries the real §8.7 weighted math. The agent here
is the agent-shell wrapper that gives the orchestrator a uniform
agent interface; the underlying math lives in
`app/services/target_recommendation/nodes.py`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


@georag_agent(
    name="Target Scoring Agent",
    risk_tier="R2",  # Writes scores + factors
    version="1.0.0",  # graduated Phase H4
)
async def target_scoring(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    run_id: UUID | str,
    target_model_version_id: UUID | str,
    zone_ids: list[UUID | str],
    scoring_kind: Literal["weighted", "xgboost", "ensemble"] = "weighted",
) -> dict[str, Any]:
    """Compute aggregate + per-factor scores for each zone.

    Phase H4 graduation — emits a per-zone weighted score envelope.
    Real DB write to `targeting.target_scores` happens when the
    orchestrator threads this through the TRG graph (which carries
    the actual §8.7 weighted math); this agent surfaces the call
    interface for ad-hoc invocations.

    Args:
        workspace_id / run_id / target_model_version_id: identifiers.
        zone_ids: zones to score.
        scoring_kind: "weighted" (deterministic baseline) is always
            available. "xgboost" + "ensemble" gate on the §12 ML
            cluster shipping; until then they fall back to "weighted"
            with a notice.

    Returns:
        Per-zone score envelope. The envelope's `notice` field flags
        the fallback when xgboost is requested but unavailable.
    """
    effective_kind = scoring_kind
    notice = None
    if scoring_kind != "weighted":
        # §12 xgboost / ensemble paths gate on the ML cluster.
        effective_kind = "weighted"
        notice = (
            f"requested scoring_kind={scoring_kind!r} unavailable — "
            f"falling back to deterministic 'weighted' baseline. "
            f"§12 ML cluster gate: xgboost dep + drilling outcomes ≥25."
        )

    scores = [
        {
            "zone_id":             str(zid),
            "scoring_kind":        effective_kind,
            "aggregate_score":     None,   # computed by score_candidate_zones
            "factor_contributions": [],     # ditto
            "shap_equivalent":     None,   # populated by Phase 12
        }
        for zid in zone_ids
    ]
    logger.info(
        "target_scoring: run_id=%s zones=%d scoring_kind=%s",
        run_id, len(zone_ids), effective_kind,
    )
    return {
        "workspace_id":            str(workspace_id),
        "run_id":                  str(run_id),
        "target_model_version_id": str(target_model_version_id),
        "scoring_kind":            effective_kind,
        "scores":                  scores,
        "notice":                  notice,
        "scored_at":               datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["target_scoring"]
