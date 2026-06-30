"""Scenario Planning Agent (§8.5 / §18.4).

Answers "what if we drilled here vs there" questions. Re-runs the
scoring pipeline with hypothetical injections (added outcomes, altered
factor weights, excluded zones) and surfaces tradeoffs against a
baseline run.

Phase H4 graduation — deterministic diff envelope. The agent computes
a structured before/after delta when handed the baseline + scenario
ranked-target lists; real graph re-execution against
`scenario_payload` plugs in when the orchestrator wires the TRG graph
through this agent.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


@georag_agent(
    name="Scenario Planning Agent",
    risk_tier="R1",  # Read + simulate; no production writes
    version="1.0.0",
)
async def scenario_planning(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    baseline_run_id: UUID | str,
    scenario_payload: dict[str, Any],
    baseline_ranked: list[dict[str, Any]] | None = None,
    scenario_ranked: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Re-score with hypothetical injections; surface tradeoffs.

    Args:
        workspace_id / project_id: RLS scope.
        baseline_run_id: the run we're comparing against.
        scenario_payload: hypothetical injections (e.g. assumed
            outcomes, factor weight overrides, AOI changes).
        baseline_ranked / scenario_ranked: optional pre-computed
            ranked target lists. When both are present, the agent
            computes the diff. When absent, returns a manifest the
            caller can use to re-execute the graph.

    Returns:
        Tradeoff envelope: per-target rank deltas + score deltas +
        zones gained / lost.
    """
    baseline_ranked = baseline_ranked or []
    scenario_ranked = scenario_ranked or []

    baseline_index = {
        str(t.get("zone_id")): t for t in baseline_ranked
    }
    scenario_index = {
        str(t.get("zone_id")): t for t in scenario_ranked
    }
    all_zone_ids = set(baseline_index) | set(scenario_index)

    deltas: list[dict[str, Any]] = []
    gained: list[str] = []
    lost: list[str] = []
    for zid in sorted(all_zone_ids):
        b = baseline_index.get(zid)
        s = scenario_index.get(zid)
        if b is None and s is not None:
            gained.append(zid)
        elif s is None and b is not None:
            lost.append(zid)
        elif b is not None and s is not None:
            deltas.append({
                "zone_id":           zid,
                "rank_baseline":     b.get("rank"),
                "rank_scenario":     s.get("rank"),
                "score_baseline":    b.get("aggregate_score"),
                "score_scenario":    s.get("aggregate_score"),
                "rank_delta":        (
                    (b.get("rank") or 0) - (s.get("rank") or 0)
                ),
                "score_delta":       (
                    (s.get("aggregate_score") or 0.0)
                    - (b.get("aggregate_score") or 0.0)
                ),
            })

    summary = (
        f"baseline_run_id={baseline_run_id} zones_compared={len(deltas)} "
        f"gained={len(gained)} lost={len(lost)} "
        f"injections={len(scenario_payload)}"
    )
    logger.info("scenario_planning: %s", summary)

    return {
        "workspace_id":      str(workspace_id),
        "project_id":        str(project_id),
        "baseline_run_id":   str(baseline_run_id),
        "scenario_payload":  scenario_payload,
        "rank_deltas":       deltas,
        "gained_zone_ids":   gained,
        "lost_zone_ids":     lost,
        "summary":           summary,
        "computed_at":       datetime.now(UTC).isoformat(),
    }


__all__ = ["scenario_planning"]
