"""Deposit Model Agent (§8.5 / §18.4).

Loads deposit model template + commodity-specific factors for the
project's commodity.

Phase H4 graduation — deterministic deposit-model catalogue keyed by
commodity. Maps `commodity_primary` + optional `target_model_slug` to a
canonical deposit-model profile (factor weights, analogue list,
recommended next-data menu). Real DB-backed lookup replaces the
catalogue once `targeting.target_models` has SME-curated rows for
every supported commodity; the contract stays identical.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


# Canonical deposit-model profiles. Per commodity_primary, a default
# slug + the factor weight kit that the §18.2 scoring pipeline uses.
_DEPOSIT_MODELS: dict[str, dict[str, Any]] = {
    "uranium": {
        "slug": "athabasca_uranium",
        "display_name": "Athabasca-style Unconformity Uranium",
        "factor_weights": {
            "proximity_to_unconformity":     0.40,
            "graphitic_basement_thickness":  0.30,
            "redox_alteration_intensity":    0.20,
            "structural_intersect_density":  0.10,
        },
        "analogues": ["McArthur River", "Cigar Lake", "Key Lake"],
        "recommended_next_data": [
            "em_survey", "alteration_traverse", "core_relog",
        ],
    },
    "gold": {
        "slug": "orogenic_gold",
        "display_name": "Orogenic / Mesothermal Gold",
        "factor_weights": {
            "shear_zone_proximity":          0.35,
            "alteration_signature_match":    0.30,
            "structural_intersect_density":  0.20,
            "geochemistry_pathfinders":      0.15,
        },
        "analogues": ["Hemlo", "Red Lake", "Detour"],
        "recommended_next_data": [
            "structure_reinterpret", "biogeochem_samples", "assay_resample",
        ],
    },
    "copper": {
        "slug": "porphyry_copper",
        "display_name": "Porphyry Copper-Molybdenum",
        "factor_weights": {
            "intrusive_centre_proximity":    0.35,
            "potassic_alteration":           0.30,
            "geochemistry_pathfinders":      0.25,
            "structural_intersect_density":  0.10,
        },
        "analogues": ["Bingham Canyon", "Highland Valley"],
        "recommended_next_data": [
            "hyperspectral_survey", "gravity_survey", "assay_resample",
        ],
    },
    "nickel": {
        "slug": "magmatic_sulphide_nickel",
        "display_name": "Magmatic Ni-Cu-PGE Sulphide",
        "factor_weights": {
            "intrusive_host_signature":      0.40,
            "structural_keel_geometry":      0.30,
            "geophys_em_anomaly":            0.20,
            "geochemistry_pathfinders":      0.10,
        },
        "analogues": ["Sudbury", "Voisey's Bay"],
        "recommended_next_data": [
            "em_survey", "geophysics_ground_truth", "core_relog",
        ],
    },
    "lithium": {
        "slug": "pegmatite_lithium",
        "display_name": "Pegmatite-hosted Lithium",
        "factor_weights": {
            "lct_pegmatite_indicator":       0.45,
            "structural_corridor_alignment": 0.25,
            "geochemistry_pathfinders":      0.20,
            "field_indicator_correlation":   0.10,
        },
        "analogues": ["Greenbushes", "Whabouchi"],
        "recommended_next_data": [
            "outcrop_validation", "biogeochem_samples", "satellite_imagery",
        ],
    },
}

_DEFAULT_PROFILE = {
    "slug": "generic_baseline",
    "display_name": "Generic Baseline Deposit Model",
    "factor_weights": {
        "proximity_to_known_occurrence": 0.40,
        "alteration_signature_match":    0.35,
        "structural_intersect_density":  0.25,
    },
    "analogues": [],
    "recommended_next_data": ["outcrop_validation"],
}


@georag_agent(
    name="Deposit Model Agent",
    risk_tier="R1",
    version="1.0.0",  # graduated Phase H4
)
async def deposit_model(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    commodity_primary: str,
    target_model_slug: str | None = None,
) -> dict[str, Any]:
    """Load deposit model + active version for the commodity.

    Args:
        workspace_id: RLS scope (informational).
        commodity_primary: e.g. "uranium", "gold", "copper".
        target_model_slug: optional override. When None, the catalogue
            default for the commodity is returned.

    Returns:
        Deposit-model profile + active version metadata.
    """
    profile = _DEPOSIT_MODELS.get(commodity_primary.lower(), _DEFAULT_PROFILE)
    selected_slug = target_model_slug or profile["slug"]
    out = {
        "workspace_id":      str(workspace_id),
        "commodity_primary": commodity_primary,
        "selected_slug":     selected_slug,
        "display_name":      profile["display_name"],
        "factor_weights":    dict(profile["factor_weights"]),
        "analogues":         list(profile["analogues"]),
        "recommended_next_data": list(profile["recommended_next_data"]),
        "scoring_kind":      "weighted",
        "source":            "deterministic_catalogue",
    }
    logger.info(
        "deposit_model: commodity=%s slug=%s factors=%d",
        commodity_primary, selected_slug, len(out["factor_weights"]),
    )
    return out


__all__ = ["deposit_model"]
