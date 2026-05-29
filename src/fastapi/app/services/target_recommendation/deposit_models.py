"""Deposit model registry (§8.2 / §20.2) — doc-phase 88 skeleton.

Ten deposit model templates ship in v1 per §20.2:

 1. Athabasca uranium (unconformity-related; primary Saskatchewan launch)
 2. Roll-front uranium (sandstone-hosted)
 3. Orogenic gold (lode gold)
 4. Epithermal gold (low-sulfidation, high-sulfidation)
 5. Porphyry copper (Cu-Mo, Cu-Au)
 6. VMS (Cu-Zn-Pb-Au-Ag)
 7. SEDEX (Pb-Zn-Ag)
 8. Lithium pegmatite
 9. Oil/gas basin (petroleum systems)
10. Custom (workspace-defined)

Each entry defines a seed structure ready for the §8.3 SME population
pass. Templates are CONSTANTS here; runtime customization happens
through `targeting.target_models` rows seeded from these templates.

SME content (§8.3) for Athabasca uranium WAITS FOR KYLE — the
template here has placeholder structure only. Other 9 models are
likewise skeletal.
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Seed structure shared across all deposit-model templates
# ---------------------------------------------------------------------------
def _seed(
    slug: str,
    display_name: str,
    commodity_primary: str,
    commodities_secondary: list[str] | None = None,
) -> dict[str, Any]:
    """Return a placeholder template structure. SME populates attributes."""
    return {
        "slug": slug,
        "display_name": display_name,
        "commodity_primary": commodity_primary,
        "commodities_secondary": commodities_secondary or [],
        # Attributes — to be populated by SME in §8.3+
        "attributes_payload": {
            "host_rocks": [],
            "structures": [],
            "alteration": [],
            "geochemistry": {
                "pathfinder_elements": [],
                "element_ratios": [],
                "anomaly_thresholds": {},
            },
            "geophysics": {
                "magnetic_signature": None,
                "radiometric_signature": None,
                "gravity_signature": None,
                "em_signature": None,
                "ip_resistivity_signature": None,
            },
            "tectonic_setting": [],
        },
        "positive_indicators": [],
        "negative_indicators": [],
        "analogues_payload": [],
        "recommended_next_data": [],
    }


# ---------------------------------------------------------------------------
# The 10 templates per §20.2
# ---------------------------------------------------------------------------
DEPOSIT_MODEL_TEMPLATES: list[dict[str, Any]] = [
    _seed(
        "athabasca_uranium",
        "Athabasca Uranium (unconformity-related)",
        "U",
        commodities_secondary=["Ni", "Co", "REE"],
    ),
    _seed(
        "roll_front_uranium",
        "Roll-Front Uranium (sandstone-hosted)",
        "U",
    ),
    _seed(
        "orogenic_gold",
        "Orogenic Gold (lode gold)",
        "Au",
        commodities_secondary=["Ag"],
    ),
    _seed(
        "epithermal_gold",
        "Epithermal Gold",
        "Au",
        commodities_secondary=["Ag"],
    ),
    _seed(
        "porphyry_copper",
        "Porphyry Copper",
        "Cu",
        commodities_secondary=["Mo", "Au"],
    ),
    _seed(
        "vms",
        "VMS (Volcanogenic Massive Sulfide)",
        "Cu",
        commodities_secondary=["Zn", "Pb", "Au", "Ag"],
    ),
    _seed(
        "sedex",
        "SEDEX (Sedimentary Exhalative)",
        "Pb",
        commodities_secondary=["Zn", "Ag"],
    ),
    _seed(
        "lithium_pegmatite",
        "Lithium Pegmatite",
        "Li",
    ),
    _seed(
        "oil_gas_basin",
        "Oil/Gas Basin (petroleum systems)",
        "oil",
        commodities_secondary=["gas"],
    ),
    _seed(
        "custom",
        "Custom (workspace-defined)",
        "custom",
    ),
]


DEPOSIT_MODEL_BY_SLUG: dict[str, dict[str, Any]] = {
    t["slug"]: t for t in DEPOSIT_MODEL_TEMPLATES
}


def get_deposit_model_template(slug: str) -> dict[str, Any]:
    """Return a deepcopy-safe template dict by slug.

    Callers mutate the returned dict freely without affecting the
    registry — `_seed` returns a fresh nested dict each call.
    """
    template = DEPOSIT_MODEL_BY_SLUG[slug]
    # Shallow-rebuild to detach the nested dicts so callers don't
    # accidentally mutate the registry. (No deepcopy import to keep
    # the dependency surface tiny.)
    return _seed(
        slug=template["slug"],
        display_name=template["display_name"],
        commodity_primary=template["commodity_primary"],
        commodities_secondary=list(template["commodities_secondary"]),
    )


__all__ = [
    "DEPOSIT_MODEL_TEMPLATES",
    "DEPOSIT_MODEL_BY_SLUG",
    "get_deposit_model_template",
]
