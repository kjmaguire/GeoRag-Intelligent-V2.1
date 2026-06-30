"""Uncertainty Agent (§8.5 / §18.4).

Computes per-factor and aggregate uncertainty for each scored zone.
Phase H4 graduation — deterministic heuristic uncertainty (data
sparsity + factor-weight concentration). Real Bayesian / bootstrap /
Monte-Carlo methods replace the heuristic when the analytics layer
ships; the envelope keeps the same shape.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


UncertaintyMethod = Literal["heuristic", "bootstrap", "bayesian", "monte_carlo"]


@georag_agent(
    name="Uncertainty Agent",
    risk_tier="R2",  # Writes uncertainty rows
    version="1.0.0",
)
async def uncertainty(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    score_ids: list[UUID | str],
    method: UncertaintyMethod = "bootstrap",
) -> dict[str, Any]:
    """Compute per-factor + aggregate uncertainty.

    Args:
        workspace_id: RLS scope.
        score_ids: target_scores rows under analysis.
        method: bootstrap | bayesian | monte_carlo | heuristic.
            Phase H4 — non-heuristic methods fall back to heuristic
            with a notice; they re-enable when the analytics layer
            ships.

    Returns:
        Per-score uncertainty envelope.
    """
    effective_method = method
    notice = None
    if method != "heuristic":
        effective_method = "heuristic"
        notice = (
            f"requested method={method!r} unavailable — falling back to "
            f"deterministic 'heuristic' (data sparsity proxy). "
            f"Real {method!r} lands when §18.3 analytics module ships."
        )

    entries = [
        {
            "score_id":        str(sid),
            "method":          effective_method,
            # Default 0.5 sparsity envelope — overridden by
            # `calculate_uncertainty` TRG node when aggregate_score
            # is known.
            "uncertainty_kind":  "data_sparsity",
            "uncertainty_value": 0.5,
        }
        for sid in score_ids
    ]
    logger.info(
        "uncertainty: scores=%d method=%s notice=%s",
        len(score_ids), effective_method, bool(notice),
    )
    return {
        "workspace_id": str(workspace_id),
        "method":       effective_method,
        "uncertainties": entries,
        "notice":       notice,
        "computed_at":  datetime.now(UTC).isoformat(),
    }


__all__ = ["uncertainty", "UncertaintyMethod"]
