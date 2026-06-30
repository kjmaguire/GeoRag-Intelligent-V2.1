"""Evidence Layer Agent (§8.5 / §18.4).

Assembles per-factor evidence (alteration, structural, geochemistry,
etc.) over the AOI for one deposit model. Each factor becomes a layer
the §8.6 ``candidate_generation`` agent reads.

Phase H4 graduation — emits a deterministic evidence-layer manifest.
Real PostGIS query + Qdrant cross-referencing replaces the stub once
the §6 hybrid retrieval layer is feature-complete; the manifest
shape is preserved.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent
from app.agents.phase8.deposit_model import (
    _DEFAULT_PROFILE,
    _DEPOSIT_MODELS,
)

logger = logging.getLogger(__name__)


def _source_kinds_for(factor_name: str) -> list[str]:
    """Map a factor name to the silver/Neo4j source kinds that feed
    it. Tunable; intentional minimal mapping that the orchestrator
    can override per-workspace."""
    mapping = {
        "alteration_signature_match":    ["silver.alterations", "silver.spatial_features"],
        "structural_intersect_density":  ["silver.structures", "neo4j:Fault"],
        "geochemistry_pathfinders":      ["silver.geochemistry", "silver.assay_results"],
        "proximity_to_known_occurrence": ["public_geo.pg_mineral_occurrence", "silver.mineral_claims"],
        "proximity_to_unconformity":     ["silver.spatial_features"],
        "graphitic_basement_thickness":  ["silver.well_log_curves", "silver.lithology_logs"],
        "redox_alteration_intensity":    ["silver.alterations", "silver.well_log_curves"],
        "intrusive_centre_proximity":    ["public_geo.pg_bedrock_geology", "silver.geological_formations"],
        "potassic_alteration":           ["silver.alterations"],
        "shear_zone_proximity":          ["silver.structures"],
        "intrusive_host_signature":      ["public_geo.pg_bedrock_geology", "silver.geochemistry"],
        "structural_keel_geometry":      ["silver.structures"],
        "geophys_em_anomaly":            ["silver.raster_layers"],
        "lct_pegmatite_indicator":       ["silver.geological_formations", "silver.geochemistry"],
        "structural_corridor_alignment": ["silver.structures"],
        "field_indicator_correlation":   ["silver.geochemistry", "silver.alterations"],
    }
    return mapping.get(factor_name, ["silver.spatial_features"])


@georag_agent(
    name="Evidence Layer Agent",
    risk_tier="R1",
    version="1.0.0",  # graduated Phase H4
)
async def evidence_layer(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    target_model_id: UUID | str,
    aoi_geom_wkt: str,
) -> dict[str, Any]:
    """Assemble per-factor evidence layers for the AOI."""
    profile = None
    for p in _DEPOSIT_MODELS.values():
        if str(p["slug"]) == str(target_model_id):
            profile = p
            break
    if profile is None:
        profile = _DEFAULT_PROFILE

    layers: list[dict[str, Any]] = []
    for factor_name in profile["factor_weights"]:
        layers.append({
            "factor_name":         factor_name,
            "source_kinds":        _source_kinds_for(factor_name),
            "aoi_intersect_count": 0,
            "layer_uri":           f"stub://layer/{factor_name}/aoi",
            "extraction_method":   "deterministic_stub",
        })

    summary = (
        f"target_model_id={target_model_id} layers={len(layers)} "
        f"aoi_wkt_len={len(aoi_geom_wkt)}"
    )
    logger.info("evidence_layer: %s", summary)

    return {
        "workspace_id":      str(workspace_id),
        "project_id":        str(project_id),
        "target_model_id":   str(target_model_id),
        "aoi_geom_wkt":      aoi_geom_wkt,
        "layers":            layers,
        "summary":           summary,
        "assembled_at":      datetime.now(UTC).isoformat(),
    }


__all__ = ["evidence_layer"]
