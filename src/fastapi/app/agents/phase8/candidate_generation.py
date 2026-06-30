"""Candidate Generation Agent (§8.5 / §18.4).

Generates candidate polygons from per-factor evidence layers using
spatial intersection + density filters. Phase H4 graduation — emits
a deterministic candidate-zone grid keyed off the layer manifest.
Real PostGIS-based generation (raster intersection, K-means clusters,
ore-body shape templates) replaces the stub when the spatial pipeline
lands.

The output shape matches what the §18.2 `score_candidate_zones` node
expects: list[{zone_id, geom_wkt, factor_seed_counts}].
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


@georag_agent(
    name="Candidate Generation Agent",
    risk_tier="R2",  # Writes candidate_zones rows
    version="1.0.0",  # graduated Phase H4
)
async def candidate_generation(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    run_id: UUID | str,
    evidence_layers: dict[str, Any],
) -> dict[str, Any]:
    """Generate candidate target polygons from evidence layers.

    Args:
        workspace_id / project_id / run_id: identifiers (informational).
        evidence_layers: output of the Evidence Layer Agent (one entry
            per factor). The candidate count scales with the layer
            count: more factors → tighter constraints → fewer zones.

    Returns:
        Candidate-zone list + per-zone factor-seed counts.
    """
    layers = evidence_layers.get("layers") or []
    n_layers = max(len(layers), 1)
    # Heuristic: 5 zones at baseline, scaled down by factor count.
    n_zones = max(3, 8 - n_layers)

    zones: list[dict[str, Any]] = []
    deltas = [(0.0, 0.0), (0.1, 0.0), (-0.1, 0.0), (0.0, 0.1), (0.0, -0.1),
              (0.05, 0.05), (-0.05, -0.05)]
    for i in range(min(n_zones, len(deltas))):
        dx, dy = deltas[i]
        wkt = (
            f"POLYGON(({dx} {dy}, {dx + 0.025} {dy}, "
            f"{dx + 0.025} {dy + 0.025}, {dx} {dy + 0.025}, "
            f"{dx} {dy}))"
        )
        zones.append({
            "zone_id":    str(uuid4()),
            "geom_wkt":   wkt,
            "factor_seed_counts": {
                layer["factor_name"]: 1 for layer in layers
            },
            "source":     "deterministic_grid",
        })

    summary = (
        f"run_id={run_id} layers={n_layers} candidate_zones={len(zones)}"
    )
    logger.info("candidate_generation: %s", summary)

    return {
        "workspace_id":     str(workspace_id),
        "project_id":       str(project_id),
        "run_id":           str(run_id),
        "candidate_zones":  zones,
        "summary":          summary,
        "generated_at":     datetime.now(UTC).isoformat(),
    }


__all__ = ["candidate_generation"]
