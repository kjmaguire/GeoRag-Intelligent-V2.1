"""Ontology term resolver (§9.2 — doc-phase 114).

First **live** (non-skeleton) ontology helper. Given a raw user-typed
or LLM-extracted string, returns the canonical term + class plus
optional payload metadata. Looks up synonyms first (case-insensitive)
and falls back to a direct canonical_term match.

Reads from `silver.geological_ontology_terms` +
`silver.geological_ontology_synonyms` — seeded by the mechanical
seeder in doc-phase 112 (47 commodities + 29 geological ages + 7
resource classes + 134 synonyms) and by the future §9.3 SME pass
for the remaining 9 classes.

Use cases:
- Entity resolution: incoming "U3O8" → ("Uranium", "commodity").
- Schema mapping: incoming "argillic" → ("Argillic Alteration",
  "alteration"). (Lands when SME populates the alteration class.)
- Hypothesis generation: surface canonical labels for graph nodes.
- Report Builder Presentation Coach: rewrite raw-data tokens to
  canonical labels.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncpg

from app.services.geological_ontology.seeds import OntologyClass


@dataclass(frozen=True, slots=True)
class ResolvedTerm:
    canonical_term: str
    ontology_class: OntologyClass
    payload: dict[str, Any]
    matched_via: str  # "synonym" | "canonical_term"


async def resolve_term(
    conn: asyncpg.Connection,
    *,
    raw_term: str,
    restrict_to_class: OntologyClass | None = None,
) -> ResolvedTerm | None:
    """Look up a raw term in the ontology.

    Lookup order:
    1. Case-insensitive exact match against `geological_ontology_synonyms.synonym`.
    2. Case-insensitive exact match against `geological_ontology_terms.canonical_term`.

    Args:
        conn: asyncpg Connection.
        raw_term: the user/LLM-typed string to resolve.
        restrict_to_class: optional — limit lookup to one ontology
            class. Useful when the caller knows it's looking for a
            commodity, not a geological age, etc.

    Returns:
        ResolvedTerm or None when nothing matches.
    """
    if not raw_term or not raw_term.strip():
        return None

    needle = raw_term.strip()

    # ---- Try synonym match first ----
    sql_synonym = """
        SELECT t.canonical_term, t.class, t.payload, 'synonym' AS matched_via
        FROM silver.geological_ontology_synonyms s
        JOIN silver.geological_ontology_terms t ON t.term_id = s.term_id
        WHERE LOWER(s.synonym) = LOWER($1)
    """
    args: list[Any] = [needle]
    if restrict_to_class is not None:
        sql_synonym += " AND t.class = $2"
        args.append(restrict_to_class)
    sql_synonym += " LIMIT 1"

    row = await conn.fetchrow(sql_synonym, *args)

    if row is None:
        # ---- Fall back to canonical_term match ----
        sql_canonical = """
            SELECT t.canonical_term, t.class, t.payload, 'canonical_term' AS matched_via
            FROM silver.geological_ontology_terms t
            WHERE LOWER(t.canonical_term) = LOWER($1)
        """
        args = [needle]
        if restrict_to_class is not None:
            sql_canonical += " AND t.class = $2"
            args.append(restrict_to_class)
        sql_canonical += " LIMIT 1"

        row = await conn.fetchrow(sql_canonical, *args)

    if row is None:
        return None

    # asyncpg returns JSONB columns as strings unless a codec is set;
    # handle both cases.
    raw_payload = row["payload"]
    if isinstance(raw_payload, str):
        import json
        payload = json.loads(raw_payload) if raw_payload else {}
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        payload = {}

    return ResolvedTerm(
        canonical_term=row["canonical_term"],
        ontology_class=row["class"],
        payload=payload,
        matched_via=row["matched_via"],
    )


async def find_synonyms(
    conn: asyncpg.Connection,
    *,
    canonical_term: str,
    ontology_class: OntologyClass | None = None,
) -> list[str]:
    """Return all synonyms for a canonical term.

    Useful for query expansion: when a user types "Uranium" and we
    want to search Qdrant for any of {U, U3O8, yellowcake, Uranium}.

    Args:
        conn: asyncpg Connection.
        canonical_term: the canonical term (case-insensitive).
        ontology_class: optional disambiguation.

    Returns:
        Sorted list of synonyms (may be empty).
    """
    if not canonical_term or not canonical_term.strip():
        return []

    sql = """
        SELECT s.synonym
        FROM silver.geological_ontology_terms t
        LEFT JOIN silver.geological_ontology_synonyms s ON s.term_id = t.term_id
        WHERE LOWER(t.canonical_term) = LOWER($1)
    """
    args: list[Any] = [canonical_term.strip()]
    if ontology_class is not None:
        sql += " AND t.class = $2"
        args.append(ontology_class)
    sql += " ORDER BY s.synonym"

    rows = await conn.fetch(sql, *args)
    return [r["synonym"] for r in rows if r["synonym"] is not None]


__all__ = ["ResolvedTerm", "resolve_term", "find_synonyms"]
