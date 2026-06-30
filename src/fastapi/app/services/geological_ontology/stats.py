"""Geological ontology stats — doc-phase 120 LIVE.

Aggregates `silver.geological_ontology_terms` + synonyms for the
admin dashboard surface — useful for tracking §9.3 SME-pass
progress. Mirror of the `get_workspace_decision_summary` pattern
from doc-phase 119 but ontology-side (no workspace scope, since
ontology is GLOBAL reference data).

Per master plan §9.3, the SME pass populates 9 of 12 classes
(deposit_model, lithology, alteration, structure, mineral_assemblage,
host_rock, tectonic_setting, geochemistry, geophysics). The 3
mechanical classes (commodity + geological_age + resource_class)
were seeded in doc-phase 112.

This helper surfaces per-class progress: term count + synonym
count + most-recent insert timestamp + status (`empty` /
`mechanical_seeded` / `sme_populating` / `populated`).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from app.services.geological_ontology.seeds import OntologyClass, seed_classes

# Per-class minimum term count thresholds for the `populated` status.
# Mechanical classes have small populations by design (e.g.,
# resource_class = 7 CIM categories total); SME classes are larger.
# These floors reflect the §9 scope proposal estimates.
_POPULATED_THRESHOLDS: dict[str, int] = {
    "commodity": 30,
    "geological_age": 20,
    "resource_class": 6,
    "deposit_model": 8,        # 10 launch types per §20.2 (target: 8+ for "populated")
    "lithology": 150,          # 200-300 entries target
    "alteration": 25,          # 30-50 alteration types
    "structure": 15,           # 20-30 structure types
    "mineral_assemblage": 20,  # "dozens used in practice"
    "host_rock": 20,
    "tectonic_setting": 12,    # 15-25 entries
    "geochemistry": 10,
    "geophysics": 10,
}

# Classes that the doc-phase 112 mechanical seeder populates. Anything
# in this set with ≥ 1 term counts as `mechanical_seeded`; SME classes
# with terms count as `sme_populating` (any) or `populated` (≥ floor).
_MECHANICAL_CLASSES = frozenset({"commodity", "geological_age", "resource_class"})


@dataclass(frozen=True, slots=True)
class OntologyClassStats:
    """Per-class progress snapshot."""

    ontology_class: str
    term_count: int
    synonym_count: int
    most_recent_term_at: datetime | None
    status: str  # 'empty' | 'mechanical_seeded' | 'sme_populating' | 'populated'
    populated_threshold: int


@dataclass(frozen=True, slots=True)
class OntologyStatsSummary:
    """Roll-up across all 12 ontology classes."""

    by_class: list[OntologyClassStats]
    total_terms: int
    total_synonyms: int
    classes_populated: int       # how many classes meet their threshold
    classes_with_any_data: int   # how many classes have ≥ 1 term
    sme_pass_complete: bool      # all 12 classes ≥ their threshold


def _classify_status(class_name: str, term_count: int) -> str:
    """Determine `empty | mechanical_seeded | sme_populating | populated`."""
    if term_count == 0:
        return "empty"
    threshold = _POPULATED_THRESHOLDS.get(class_name, 1)
    if term_count >= threshold:
        return "populated"
    if class_name in _MECHANICAL_CLASSES:
        return "mechanical_seeded"
    return "sme_populating"


async def get_ontology_class_stats(
    conn: asyncpg.Connection,
    *,
    only_classes: Iterable[OntologyClass] | None = None,
) -> OntologyStatsSummary:
    """Compute per-class + roll-up stats for the geological ontology.

    Args:
        conn: asyncpg Connection.
        only_classes: optional restrict — list of class names to include.
            Defaults to all 12 §20.1 classes.

    Returns:
        `OntologyStatsSummary` with per-class breakdown + totals.
    """
    target_classes = list(only_classes) if only_classes else seed_classes()

    rows = await conn.fetch(
        """
        WITH per_class AS (
            SELECT
                t.class,
                count(*) AS term_count,
                max(t.created_at) AS most_recent
            FROM silver.geological_ontology_terms t
            WHERE t.class = ANY($1::text[])
            GROUP BY t.class
        ),
        per_class_syns AS (
            SELECT
                t.class,
                count(*) AS synonym_count
            FROM silver.geological_ontology_synonyms s
            JOIN silver.geological_ontology_terms t ON t.term_id = s.term_id
            WHERE t.class = ANY($1::text[])
            GROUP BY t.class
        )
        SELECT
            c.class,
            COALESCE(p.term_count, 0)    AS term_count,
            COALESCE(s.synonym_count, 0) AS synonym_count,
            p.most_recent
        FROM unnest($1::text[]) AS c(class)
        LEFT JOIN per_class p ON p.class = c.class
        LEFT JOIN per_class_syns s ON s.class = c.class
        ORDER BY c.class
        """,
        list(target_classes),
    )

    by_class: list[OntologyClassStats] = []
    total_terms = 0
    total_synonyms = 0
    classes_populated = 0
    classes_with_any_data = 0

    for r in rows:
        class_name = r["class"]
        term_count = int(r["term_count"])
        synonym_count = int(r["synonym_count"])
        threshold = _POPULATED_THRESHOLDS.get(class_name, 1)
        status = _classify_status(class_name, term_count)

        by_class.append(OntologyClassStats(
            ontology_class=class_name,
            term_count=term_count,
            synonym_count=synonym_count,
            most_recent_term_at=r["most_recent"],
            status=status,
            populated_threshold=threshold,
        ))

        total_terms += term_count
        total_synonyms += synonym_count
        if term_count > 0:
            classes_with_any_data += 1
        if status == "populated":
            classes_populated += 1

    sme_pass_complete = (
        classes_populated == len(target_classes)
    )

    return OntologyStatsSummary(
        by_class=by_class,
        total_terms=total_terms,
        total_synonyms=total_synonyms,
        classes_populated=classes_populated,
        classes_with_any_data=classes_with_any_data,
        sme_pass_complete=sme_pass_complete,
    )


__all__ = [
    "OntologyClassStats",
    "OntologyStatsSummary",
    "get_ontology_class_stats",
]
