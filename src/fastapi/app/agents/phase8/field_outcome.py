"""Field Outcome Agent (§8.5 / §18.4).

Ingests post-drilling outcomes (drillhole assays, true target hit/miss
status, lessons learned) and emits the structured row that gets
inserted into `targeting.target_outcomes`. The `field_outcome_learning`
Hatchet workflow (graduated doc-phase 184) consumes these rows to
update model-version learning state.

Phase H4 graduation — pure-function over inputs; emits the row
envelope ready for INSERT.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


HitOrMiss = Literal["hit", "miss", "partial", "abandoned", "deferred"]


@georag_agent(
    name="Field Outcome Agent",
    risk_tier="R2",  # Writes outcome rows
    version="1.0.0",
)
async def field_outcome(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    recommendation_id: UUID | str,
    drillhole_collar_id: UUID | str | None,
    hit_or_miss: HitOrMiss,
    outcome_payload: dict[str, Any],
) -> dict[str, Any]:
    """Record a drilled-target outcome against a past recommendation.

    Args:
        workspace_id: RLS scope.
        recommendation_id: the `targeting.target_recommendations` row.
        drillhole_collar_id: the silver.collars row that drove the
            outcome (None if outcome was abandoned before drilling).
        hit_or_miss: classification.
        outcome_payload: free-form structured data (assay summary,
            grade × thickness, geometric controls, etc.).

    Returns:
        Outcome row envelope ready for INSERT into
        `targeting.target_outcomes`.
    """
    outcome_id = uuid4()
    envelope = {
        "outcome_id":           str(outcome_id),
        "workspace_id":         str(workspace_id),
        "recommendation_id":    str(recommendation_id),
        "drillhole_collar_id":  (
            str(drillhole_collar_id) if drillhole_collar_id else None
        ),
        "hit_or_miss":          hit_or_miss,
        "outcome_payload":      outcome_payload,
        "recorded_at":          datetime.now(timezone.utc).isoformat(),
        "audit_action_type":    f"target.outcome.{hit_or_miss}",
    }
    logger.info(
        "field_outcome: recommendation=%s outcome_id=%s hit_or_miss=%s",
        recommendation_id, outcome_id, hit_or_miss,
    )
    return envelope


__all__ = ["field_outcome", "HitOrMiss"]
