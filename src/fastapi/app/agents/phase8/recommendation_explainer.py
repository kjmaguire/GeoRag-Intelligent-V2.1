"""Recommendation Explainer Agent (§8.5 / §8.12 / §18.4).

Drafts per-target rationale that:
  - explains the score via top factor contributions
  - flags uncertainty drivers
  - cites the evidence chunks that drove each factor
  - speaks geological-narrative tone (not raw factor numbers)

Phase H4 graduation — deterministic Markdown template + factor
sentence assembler. The §25.4 LLM-driven explainer (with prompt locks
+ geological tone enforcement) plugs in here when prompt-lock ships.
The output schema is identical so callers don't change.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


def _factor_sentence(factor: dict[str, Any]) -> str:
    name = factor.get("factor_name", "<unknown>")
    contrib = float(factor.get("contribution", 0.0))
    direction = "boosting" if contrib >= 0 else "dragging"
    return (
        f"- **{name}** ({direction} by {abs(contrib):.3f}) — evidence: "
        f"{len(factor.get('evidence_chunk_ids') or [])} cited chunk(s)."
    )


@georag_agent(
    name="Recommendation Explainer Agent",
    risk_tier="R2",  # Drafts language for downstream R5 sign-off
    version="1.0.0",
)
async def recommendation_explainer(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    zone_id: UUID | str,
    score_id: UUID | str,
    rank: int,
    factor_breakdown: list[dict[str, Any]],
) -> dict[str, Any]:
    """Draft per-target rationale.

    Args:
        workspace_id: RLS scope.
        zone_id / score_id: the target under explanation.
        rank: 1-based rank in the run's ranked_targets list.
        factor_breakdown: list of factors with contribution + evidence.

    Returns:
        Markdown rationale + structured top/bottom factor flags.
    """
    if not factor_breakdown:
        rationale = (
            f"### Rank #{rank} — Zone {zone_id}\n\n"
            f"*No factor breakdown available; the §18.2 scoring node "
            f"hasn't produced contributions for this score yet.*"
        )
        return {
            "workspace_id":         str(workspace_id),
            "zone_id":              str(zone_id),
            "score_id":             str(score_id),
            "rank":                 rank,
            "rationale_markdown":   rationale,
            "top_factor":           None,
            "drag_factor":          None,
            "explained_at":         datetime.now(UTC).isoformat(),
        }

    sorted_factors = sorted(
        factor_breakdown, key=lambda f: float(f.get("contribution", 0.0)),
        reverse=True,
    )
    top = sorted_factors[0]
    drag = sorted_factors[-1] if sorted_factors[-1] is not top else None

    sentences = "\n".join(_factor_sentence(f) for f in sorted_factors)

    rationale = (
        f"### Rank #{rank} — Zone {zone_id}\n\n"
        f"This zone scored prominently in the §18.2 weighted aggregation. "
        f"Top contributing factor: **{top.get('factor_name')}** "
        f"({float(top.get('contribution', 0.0)):.3f}).\n\n"
        f"**Per-factor breakdown:**\n\n{sentences}\n\n"
    )
    if drag:
        rationale += (
            f"**Drag on the score:** **{drag.get('factor_name')}** "
            f"({float(drag.get('contribution', 0.0)):.3f}). Consider "
            f"acquiring next-best-data to lift this factor.\n"
        )

    return {
        "workspace_id":       str(workspace_id),
        "zone_id":            str(zone_id),
        "score_id":           str(score_id),
        "rank":               rank,
        "rationale_markdown": rationale,
        "top_factor":         top.get("factor_name"),
        "drag_factor":        drag.get("factor_name") if drag else None,
        "explained_at":       datetime.now(UTC).isoformat(),
    }


__all__ = ["recommendation_explainer"]
