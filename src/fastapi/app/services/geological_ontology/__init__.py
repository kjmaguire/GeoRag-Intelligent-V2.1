"""Geological ontology — §9.1 + §9.2 — doc-phase 90 / 114.

Per master-plan §20.1, the ontology covers 12 classes (11 in §20.1 +
1 reference class resource_class). This package provides:

- `ONTOLOGY_CLASS_SEEDS` — per-class empty term lists ready for the
  §9.3 SME pass.
- `OntologyClass` Literal type — type-safe class names.
- `seed_classes()` — list the 12 canonical class names.
- `resolve_term()` — **live** async lookup (doc-phase 114). Resolves
  raw strings against synonyms + canonical_term. Works against the
  mechanical-seeded classes today (commodity, geological_age,
  resource_class — 83 terms + 134 synonyms).
- `find_synonyms()` — **live** async helper for query expansion.

SME content (§9.3) for the remaining 9 classes is the multi-week
external-contractor or internal SME pass.

Live usage:

    from app.services.geological_ontology import resolve_term
    r = await resolve_term(conn, raw_term="U3O8")
    # → ResolvedTerm(canonical_term="Uranium", ontology_class="commodity",
    #                payload={"element_symbol": "U"}, matched_via="synonym")
"""
from app.services.geological_ontology.resolver import (
    ResolvedTerm,
    find_synonyms,
    resolve_term,
)
from app.services.geological_ontology.seeds import (
    ONTOLOGY_CLASS_SEEDS,
    OntologyClass,
    seed_classes,
)
from app.services.geological_ontology.stats import (
    OntologyClassStats,
    OntologyStatsSummary,
    get_ontology_class_stats,
)

__all__ = [
    "ONTOLOGY_CLASS_SEEDS",
    "OntologyClass",
    "OntologyClassStats",
    "OntologyStatsSummary",
    "ResolvedTerm",
    "find_synonyms",
    "get_ontology_class_stats",
    "resolve_term",
    "seed_classes",
]
