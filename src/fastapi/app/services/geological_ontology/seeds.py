"""Ontology class seeds (§9.2) — doc-phase 90 skeleton.

The 12 ontology classes from §20.1. Each `seeds[class]` is a list
of canonical-term dicts ready for population by Kyle's §9.3 SME pass.

Default seeds are empty — the §9.3 SME population pass fills these
in. Where the master plan calls out specific examples (e.g.
"Athabasca uranium" under deposit_model, "argillic" under alteration),
they're listed as TODOs in the doc strings below for the SME
contractor's reference.
"""
from __future__ import annotations

from typing import Any, Literal

OntologyClass = Literal[
    "deposit_model",
    "commodity",
    "lithology",
    "alteration",
    "structure",
    "mineral_assemblage",
    "host_rock",
    "geological_age",
    "tectonic_setting",
    "geochemistry",
    "geophysics",
    "resource_class",
]


# Notes per class — what the SME pass should populate
ONTOLOGY_CLASS_NOTES: dict[OntologyClass, str] = {
    "deposit_model": (
        "10 launch types per §20.2 (Athabasca uranium, roll-front uranium, "
        "orogenic gold, epithermal gold, porphyry copper, VMS, SEDEX, "
        "lithium pegmatite, oil/gas basin, custom). Each gets full synonym set."
    ),
    "commodity": (
        "Periodic-table-grade list: U, Au, Cu, Ni, Co, Li, Zn, Pb, Mo, Ag, "
        "REE, plus secondary commodities (Sb, As, Sn, W, Be, etc.). "
        "Synonyms include element symbols + common names."
    ),
    "lithology": (
        "BGS Rock Classification Scheme mapping. Granite, sandstone, schist, "
        "gneiss, etc. Estimate: 200-300 entries for full coverage."
    ),
    "alteration": (
        "Argillic, phyllic, propylitic, sericitic, hematitic, chloritic, "
        "potassic, etc. Estimate: 30-50 alteration types."
    ),
    "structure": (
        "Fault, shear zone, fold, fracture, vein, contact, lineament. "
        "Estimate: 20-30 structure types with kinematic synonyms."
    ),
    "mineral_assemblage": (
        "Pyrite-chalcopyrite, sericite-pyrite, etc. Hundreds of combinations; "
        "Kyle/contractor curates the dozens used in practice."
    ),
    "host_rock": (
        "Cross-references lithology + deposit_model. Athabasca-style "
        "sandstone-unconformity, orogenic-gold-greenstone-belt, etc."
    ),
    "geological_age": (
        "Archean, Proterozoic, Paleozoic, Mesozoic, Cenozoic + finer "
        "subdivisions (Jurassic, Cretaceous, etc.). Estimate: 50-80 entries."
    ),
    "tectonic_setting": (
        "Convergent margin, rift, intracratonic basin, passive margin, "
        "back-arc, fore-arc, etc. Estimate: 15-25 entries."
    ),
    "geochemistry": (
        "Pathfinder elements per commodity + element ratios. Stored as "
        "structured payload (anomaly_thresholds dict)."
    ),
    "geophysics": (
        "Signature patterns for each method (magnetic, radiometric, gravity, "
        "EM, IP/resistivity). Stored as structured payload."
    ),
    "resource_class": (
        "CIM standard: measured, indicated, inferred, mineral resource, "
        "mineral reserve, probable reserve, proven reserve."
    ),
}


# Empty seeds per class. SME populates.
ONTOLOGY_CLASS_SEEDS: dict[OntologyClass, list[dict[str, Any]]] = {
    cls: [] for cls in ONTOLOGY_CLASS_NOTES
}


def seed_classes() -> list[OntologyClass]:
    """Return the 12 canonical ontology class names."""
    return list(ONTOLOGY_CLASS_NOTES.keys())


__all__ = [
    "OntologyClass",
    "ONTOLOGY_CLASS_NOTES",
    "ONTOLOGY_CLASS_SEEDS",
    "seed_classes",
]
