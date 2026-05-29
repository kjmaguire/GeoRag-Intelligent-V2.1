"""Graph-assisted entity disambiguation (Eval 14 R3 follow-up).

Geological queries often reference entities by ambiguous names:
"the main zone", "Zone 3", "Athabasca Sandstone". Without
disambiguation, the vector search retrieves passages about
different formations that share a name fragment — diluting
relevance and risking cross-context confusion.

This module sits in front of retrieval. Given a query string, it
extracts candidate formation names and resolves them to canonical
Formation nodes in Neo4j via the ``formation_name_fts`` full-text
index. The resolved nodes' canonical names + project_ids feed the
Qdrant `additional_filter` so the dense+sparse search is biased
toward passages about those formations.

The fulltext index is created by ``scripts/populate_neo4j.py``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Regex for likely formation references: capitalised multi-word phrases,
# "Zone N" patterns, and "the X Formation/Sandstone/Group" patterns.
# Conservative — false negatives are recoverable (the vector search
# still works); false positives risk over-constraining the filter.
_FORMATION_PATTERNS = [
    re.compile(r"\b(?:Zone\s+[A-Z0-9]+)\b"),
    re.compile(
        r"\b((?:[A-Z][a-z]+\s+){1,3}"
        r"(?:Formation|Sandstone|Group|Member|Conglomerate|Shale|Granite))\b"
    ),
    re.compile(r"\bthe\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+(?:zone|deposit|orebody)\b"),
]


@dataclass(frozen=True)
class ResolvedEntity:
    """A formation candidate matched against the Neo4j graph."""

    surface_form: str            # The exact span pulled from the query
    canonical_name: str          # Formation node's `name` property
    confidence: float            # 0..1 from Lucene relevance
    formation_type: str | None   # company / basin / county / unknown


def extract_candidate_terms(query: str) -> list[str]:
    """Lift formation-shaped substrings out of a free-text query.

    Returns deduplicated, original-case spans. Empty list when no
    candidate is found — the caller should NOT filter retrieval in
    that case (we'd be biasing on nothing).
    """
    seen: set[str] = set()
    out: list[str] = []
    for pat in _FORMATION_PATTERNS:
        for match in pat.finditer(query):
            # group(1) when present, else group(0)
            term = match.group(1) if match.groups() else match.group(0)
            term = term.strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                out.append(term)
    return out


async def resolve_formation_terms(
    neo4j_driver: object | None,
    query: str,
    *,
    limit_per_term: int = 3,
    timeout_s: float = 1.5,
) -> list[ResolvedEntity]:
    """Resolve candidate terms to canonical Formation nodes.

    Best-effort: returns [] when Neo4j is unavailable, when the FTS
    index doesn't exist, or when extract_candidate_terms returns no
    candidates. The orchestrator's retrieval path uses the result
    as a relevance boost, never as a hard filter — Neo4j outage
    must not block the chat query.
    """
    if neo4j_driver is None:
        return []
    terms = extract_candidate_terms(query)
    if not terms:
        return []

    cypher = (
        "CALL db.index.fulltext.queryNodes('formation_name_fts', $term) "
        "YIELD node, score "
        "RETURN node.name AS name, score, "
        "       node.formation_type AS formation_type "
        "ORDER BY score DESC "
        "LIMIT $limit"
    )

    resolved: list[ResolvedEntity] = []
    try:
        async with neo4j_driver.session() as session:  # type: ignore[union-attr]
            for term in terms:
                try:
                    result = await asyncio.wait_for(
                        session.run(cypher, term=term, limit=limit_per_term),
                        timeout=timeout_s,
                    )
                    records = await asyncio.wait_for(
                        result.data(), timeout=timeout_s,
                    )
                except (asyncio.TimeoutError, Exception):
                    logger.debug(
                        "resolve_formation_terms: lookup failed term=%s",
                        term, exc_info=True,
                    )
                    continue

                if not records:
                    continue
                top_score = records[0].get("score") or 0.0
                max_score = max(top_score, 0.001)
                for r in records:
                    score = r.get("score") or 0.0
                    resolved.append(
                        ResolvedEntity(
                            surface_form=term,
                            canonical_name=r.get("name") or term,
                            confidence=float(score) / float(max_score),
                            formation_type=r.get("formation_type"),
                        )
                    )
    except Exception:
        logger.debug(
            "resolve_formation_terms: Neo4j session failed", exc_info=True,
        )
        return []

    logger.info(
        "resolve_formation_terms: query had %d candidate(s), resolved %d",
        len(terms), len(resolved),
    )
    return resolved


__all__ = [
    "ResolvedEntity",
    "extract_candidate_terms",
    "resolve_formation_terms",
]
