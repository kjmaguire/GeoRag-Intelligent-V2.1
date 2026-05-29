"""Athabasca uranium deposit model — §8.3 SME content (doc-phase 123).

═══════════════════════════════════════════════════════════════════
KYLE — FILL IN THIS FILE
═══════════════════════════════════════════════════════════════════

Per master-plan §8.3 + §20.2, this module holds the SME-authored
attributes for the Athabasca uranium (unconformity-related uranium)
deposit model — the launch model for the Saskatchewan rollout.

Every entry below has a TODO placeholder + an example shape. Edit
each one with the value(s) you want; remove the TODO marker once
filled. The seeder will REFUSE to run while any required block
still contains a TODO marker — that's intentional so we never land
half-curated R5 reference data.

Structure mirrors the §20.2 `deposit_models entry` schema verbatim.

After editing, run:

    docker exec georag-fastapi python -m \\
        app.services.target_recommendation.sme_content \\
        --slug athabasca_uranium --activate

═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

SLUG = "athabasca_uranium"
DISPLAY_NAME = "Athabasca Uranium (unconformity-related)"
COMMODITY_PRIMARY = "U"
COMMODITIES_SECONDARY = ["Ni", "Co", "REE"]  # Common Athabasca by-products


# ---------------------------------------------------------------------------
# attributes_payload — §20.2 structure
# ---------------------------------------------------------------------------

# TODO(Kyle): Host rock types where Athabasca-style mineralization occurs.
# Strings — names + descriptions. Examples of the shape (NOT geological
# guidance — these are placeholders to show the format):
#   "Athabasca Group sandstone — Manitou Falls Formation"
#   "Basement gneiss — Wollaston Domain"
#   "Quartz-pebble conglomerate of the Read Formation"
HOST_ROCKS: list[str] = [
    # TODO: add Athabasca host rock types
]

# TODO(Kyle): Structural controls. Strings describing structural features
# associated with mineralization. Shape examples:
#   "Reactivated basement faults"
#   "Graphitic shear zones"
#   "Sub-unconformity basement faults intersecting the regolith"
#   "Brittle-ductile fault zones with paleo-fluid evidence"
STRUCTURES: list[str] = [
    # TODO: add structural controls
]

# TODO(Kyle): Alteration assemblages. Strings — assemblage names + brief
# qualifiers. Shape examples:
#   "Clay alteration — illite + chlorite + dravite (Mg-tourmaline)"
#   "Sudoite (Mg-Al chlorite) halo"
#   "Hematitization with associated reduction zones"
#   "Silicification at unconformity contact"
ALTERATION: list[str] = [
    # TODO: add alteration assemblages
]

# TODO(Kyle): Pathfinder elements. Element symbols — the elements whose
# anomalies indicate proximity to Athabasca-style mineralization.
# Shape examples:
#   ["U", "Pb", "Mo", "Cu", "Ni", "Co", "As", "B"]
GEOCHEMISTRY_PATHFINDER_ELEMENTS: list[str] = [
    # TODO: add pathfinder elements
]

# TODO(Kyle): Diagnostic element ratios. Strings describing the ratio +
# threshold. Shape examples:
#   "Pb/U > 1 indicates radiogenic Pb leakage from buried orebody"
#   "B/Cl > 5 marks tourmaline-rich alteration halo"
GEOCHEMISTRY_ELEMENT_RATIOS: list[str] = [
    # TODO: add diagnostic ratios
]

# TODO(Kyle): Anomaly thresholds. Dict mapping element_unit → threshold value.
# Shape examples:
#   {"U_ppm_min": 100, "B_ppm_min": 50, "As_ppm_min": 30}
GEOCHEMISTRY_ANOMALY_THRESHOLDS: dict[str, float] = {
    # TODO: add anomaly thresholds per element
}

# TODO(Kyle): Geophysical signatures, one string per method.
# Empty string means "no diagnostic signature for this method" — leave
# blank rather than guess. Examples of shape:
GEOPHYSICS_MAGNETIC_SIGNATURE: str = ""  # TODO
GEOPHYSICS_RADIOMETRIC_SIGNATURE: str = ""  # TODO
GEOPHYSICS_GRAVITY_SIGNATURE: str = ""  # TODO
GEOPHYSICS_EM_SIGNATURE: str = ""  # TODO
GEOPHYSICS_IP_RESISTIVITY_SIGNATURE: str = ""  # TODO

# TODO(Kyle): Tectonic setting strings. Shape examples:
#   "Intracratonic Proterozoic basin (Athabasca Basin, Mesoproterozoic)"
#   "Trans-Hudson Orogen basement (Hearne Craton + Wollaston Domain)"
TECTONIC_SETTING: list[str] = [
    # TODO: add tectonic settings
]


# ---------------------------------------------------------------------------
# Indicators — features that adjust the target score
# ---------------------------------------------------------------------------

# TODO(Kyle): Positive indicators — features whose PRESENCE raises target
# score. Strings. Shape examples:
#   "Unconformity intersection with reactivated basement fault"
#   "Co-located clay alteration + graphitic shear zone"
#   "Radiometric U anomaly within 500 m of fault intersection"
POSITIVE_INDICATORS: list[str] = [
    # TODO: add positive indicators
]

# TODO(Kyle): Negative indicators — features whose PRESENCE lowers target
# score. Strings. Shape examples:
#   "Greater than 1 km thickness of unweathered sandstone with no faults"
#   "Distal from any basement structure"
#   "No graphitic units in basement"
NEGATIVE_INDICATORS: list[str] = [
    # TODO: add negative indicators
]


# ---------------------------------------------------------------------------
# Known analogues — examples geologists can refer back to
# ---------------------------------------------------------------------------

# TODO(Kyle): Known Athabasca-style deposits as analogues. Each entry is a
# dict with the fields below. Shape example:
#   {
#       "name": "Cigar Lake",
#       "operator": "Cameco / Orano",
#       "location": "Saskatchewan, Athabasca Basin",
#       "discovered_year": 1981,
#       "resource_summary": "≈ 217 Mlb U3O8 @ 17.8% U3O8 — among the world's largest + highest-grade",
#       "key_features": ["Unconformity-hosted", "Sub-vertical fault-controlled", "Clay-altered halo"],
#       "notes": "Production resumed 2014 after freeze-pad remediation",
#   }
ANALOGUES: list[dict] = [
    # TODO: add analogue dicts
]


# ---------------------------------------------------------------------------
# Recommended next-best-data menu (§20.5 cross-reference)
# ---------------------------------------------------------------------------

# TODO(Kyle): Recommended next data acquisitions, ranked by expected
# uncertainty reduction. Each entry is a dict. Shape example:
#   {
#       "kind": "em_survey",                 # one of the 14 §20.5 kinds
#       "scope": "VTEM-Max over fault corridor X",
#       "rationale": "Identifies graphitic conductors at depth",
#       "estimated_cost_usd": [80_000, 150_000],
#       "estimated_time_days": [14, 30],
#       "expected_uncertainty_reduction": 0.20,  # 0-1
#       "prerequisites": ["AOI defined", "ground access permits"],
#   }
RECOMMENDED_NEXT_DATA: list[dict] = [
    # TODO: add next-best-data entries
]


# ---------------------------------------------------------------------------
# Scoring weights — §8.7 deliverable
# ---------------------------------------------------------------------------

# TODO(Kyle): Per-factor weights for the §8.7 weighted scoring formula.
# Weights are dimensionless multipliers. The §18.3 baseline formula is:
#   target_score =
#     alteration_factor   * w_alteration +
#     structural_factor   * w_structural +
#     geochemistry_factor * w_geochem +
#     proximity_factor    * w_proximity +
#     geophysics_factor   * w_geophysics +
#     analogue_factor     * w_analogue
#
# Common starting point: weights sum to 1.0. Adjust based on which
# evidence types are most diagnostic for THIS deposit model.
SCORING_WEIGHTS: dict[str, float] = {
    "alteration":   0.0,  # TODO
    "structural":   0.0,  # TODO
    "geochemistry": 0.0,  # TODO
    "proximity":    0.0,  # TODO
    "geophysics":   0.0,  # TODO
    "analogue":     0.0,  # TODO
}


# ---------------------------------------------------------------------------
# Assembly — the seeder reads this. DO NOT EDIT BELOW.
# ---------------------------------------------------------------------------

def get_content() -> dict:
    """Return the full content dict the seeder consumes."""
    return {
        "slug": SLUG,
        "display_name": DISPLAY_NAME,
        "commodity_primary": COMMODITY_PRIMARY,
        "commodities_secondary": COMMODITIES_SECONDARY,
        "attributes_payload": {
            "host_rocks": HOST_ROCKS,
            "structures": STRUCTURES,
            "alteration": ALTERATION,
            "geochemistry": {
                "pathfinder_elements": GEOCHEMISTRY_PATHFINDER_ELEMENTS,
                "element_ratios": GEOCHEMISTRY_ELEMENT_RATIOS,
                "anomaly_thresholds": GEOCHEMISTRY_ANOMALY_THRESHOLDS,
            },
            "geophysics": {
                "magnetic_signature": GEOPHYSICS_MAGNETIC_SIGNATURE,
                "radiometric_signature": GEOPHYSICS_RADIOMETRIC_SIGNATURE,
                "gravity_signature": GEOPHYSICS_GRAVITY_SIGNATURE,
                "em_signature": GEOPHYSICS_EM_SIGNATURE,
                "ip_resistivity_signature": GEOPHYSICS_IP_RESISTIVITY_SIGNATURE,
            },
            "tectonic_setting": TECTONIC_SETTING,
        },
        "positive_indicators": POSITIVE_INDICATORS,
        "negative_indicators": NEGATIVE_INDICATORS,
        "analogues_payload": ANALOGUES,
        "recommended_next_data": RECOMMENDED_NEXT_DATA,
        "scoring_weights": SCORING_WEIGHTS,
    }


def is_populated() -> tuple[bool, list[str]]:
    """Return (ready, blockers) — `ready` is True only when no required
    field is empty / zeroed.

    The seeder calls this BEFORE writing — refuses to seed half-curated
    content per the §8.3 R5 sign-off story.
    """
    blockers: list[str] = []

    if not HOST_ROCKS:
        blockers.append("HOST_ROCKS is empty")
    if not STRUCTURES:
        blockers.append("STRUCTURES is empty")
    if not ALTERATION:
        blockers.append("ALTERATION is empty")
    if not GEOCHEMISTRY_PATHFINDER_ELEMENTS:
        blockers.append("GEOCHEMISTRY_PATHFINDER_ELEMENTS is empty")
    if not TECTONIC_SETTING:
        blockers.append("TECTONIC_SETTING is empty")
    if not POSITIVE_INDICATORS:
        blockers.append("POSITIVE_INDICATORS is empty")
    if not NEGATIVE_INDICATORS:
        blockers.append("NEGATIVE_INDICATORS is empty")
    if not ANALOGUES:
        blockers.append("ANALOGUES is empty — at least 1 known deposit required")

    # Scoring weights must sum to > 0 (not all zero) — otherwise no
    # scoring path is viable.
    if sum(SCORING_WEIGHTS.values()) == 0:
        blockers.append(
            "SCORING_WEIGHTS all zero — at least one factor weight must be set"
        )

    return (len(blockers) == 0, blockers)
