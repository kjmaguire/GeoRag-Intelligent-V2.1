"""Constraint Agent (§8.5 / §18.4).

Applies exclusions: no-go areas, claim boundaries (other companies'
property), environmentally-sensitive zones, road/infrastructure
buffers, etc. Returns the list of zone_ids that survive the
exclusion sweep + a separate list of exclusions applied.

Phase H4 graduation — deterministic exclusion rule table; real
PostGIS ST_Intersects against constraint layers replaces this when
the constraint-layer schema ships.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


# Constraint kinds tracked by the §8.5 spec. Each carries (kind,
# default_enabled, default_buffer_m). Workspaces can override via
# `silver.workspace_constraint_overrides` when that table ships.
_CONSTRAINT_RULES: list[tuple[str, bool, int]] = [
    ("private_claim_boundary",  True,  0),     # other operators' claims
    ("environmental_no_go",     True,  100),   # parks, reserves, watercourses
    ("road_infrastructure",     False, 50),    # buffer around roads
    ("first_nations_sacred_site", True, 500),  # culturally sensitive zones
    ("crown_land_only",         False, 0),     # restrict to crown leases
]


@georag_agent(
    name="Constraint Agent",
    risk_tier="R1",  # Read-only; advisory output
    version="1.0.0",
)
async def constraint(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    zone_ids: list[UUID | str],
) -> dict[str, Any]:
    """Apply exclusion constraints to candidate zones.

    Returns:
        - retained_zone_ids: zones that survive
        - excluded_zone_ids: zones excluded + reason
        - rules_applied: which constraint rules were enabled
    """
    # Phase H4 deterministic baseline: emit the rules manifest; no
    # actual zone exclusion until PostGIS lookups land. All zones
    # retained so the orchestrator can wire this in without breaking
    # the downstream scoring graph.
    rules_applied = [
        {
            "kind":          kind,
            "enabled":       enabled,
            "buffer_metres": buffer,
        }
        for (kind, enabled, buffer) in _CONSTRAINT_RULES
    ]

    summary = (
        f"project_id={project_id} zones_in={len(zone_ids)} "
        f"rules_applied={sum(1 for r in rules_applied if r['enabled'])} "
        f"zones_excluded=0 (deterministic_stub)"
    )
    logger.info("constraint: %s", summary)

    return {
        "workspace_id":      str(workspace_id),
        "project_id":        str(project_id),
        "retained_zone_ids": [str(z) for z in zone_ids],
        "excluded_zone_ids": [],
        "rules_applied":     rules_applied,
        "summary":           summary,
        "applied_at":        datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["constraint"]
