"""Spatial Relationship Agent (§9.6 / §20.4).

PostGIS + Neo4j combined queries to answer "what does this fault
imply about this anomaly" / "what does this intrusive contact
suggest about hosting" / "what's the relationship between this
alteration zone and this structure" — geological-meaning queries
that pure spatial-overlay can't answer.

Phase H4 graduation — emits a deterministic relationship envelope
keyed off ``subject_entity_id``. When the caller supplies a pre-fetched
``relationships`` list (typically from a Cypher query in the §11
orchestrator), the agent normalises + filters + scores them. Without
input, returns an empty list + a manifest of which predicates would
be queried.

The agent itself doesn't issue the Neo4j call — that lives in the
§11 retrieval orchestrator, which has the driver + tenant scoping.
This keeps the agent pure-function for testing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


# Geological predicates the §20.4 catalogue supports. Defaults to
# the full set; caller can narrow via predicate_filter.
_KNOWN_PREDICATES: tuple[str, ...] = (
    "crosscuts",          # one structure cuts another
    "hosts",              # rock unit hosts a deposit / showing
    "overprints",         # later event modifies earlier
    "is_in",              # spatial containment
    "intersects",         # overlap without containment
    "adjacent_to",        # share a boundary
    "parallel_to",        # share orientation
    "near",               # within proximity buffer
    "post_dates",         # temporal sequence
    "ancestral_to",       # tectonic descent
)


def _confidence_for(rel: dict[str, Any]) -> float:
    """Per-relationship confidence. Caller-provided value wins; else
    we estimate from evidence count + the relationship's explicit
    confidence field if present."""
    raw = rel.get("confidence")
    if isinstance(raw, (int, float)) and 0.0 <= raw <= 1.0:
        return float(raw)
    evidence = rel.get("evidence_chunk_ids") or []
    if not evidence:
        return 0.5
    # 0.6 floor + 0.05 per chunk, capped at 0.95
    return min(0.95, 0.6 + 0.05 * len(evidence))


def _normalise(rel: dict[str, Any], subject_id: str) -> dict[str, Any]:
    return {
        "subject_id":         str(rel.get("subject_id", subject_id)),
        "predicate":          str(rel.get("predicate", "near")),
        "object_id":          str(rel.get("object_id", "")),
        "evidence_chunk_ids": list(rel.get("evidence_chunk_ids") or []),
        "confidence":         _confidence_for(rel),
    }


@georag_agent(
    name="Spatial Relationship Agent",
    risk_tier="R1",
    version="1.0.0",  # graduated Phase H4
)
async def spatial_relationship(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    subject_entity_id: str,
    predicate_filter: list[str] | None = None,
    relationships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Discover geological relationships for a subject entity.

    Args:
        workspace_id / project_id: RLS scope.
        subject_entity_id: the entity (drillhole id, fault id, etc.)
            we're exploring outward from.
        predicate_filter: optional whitelist of predicates. None =
            return all predicates.
        relationships: optional pre-fetched relationship list. Each
            entry carries ``predicate``, ``object_id``,
            ``evidence_chunk_ids``, optional ``confidence``. Production
            sets this from a Neo4j Cypher query in the orchestrator;
            tests pass it directly.

    Returns:
        Relationships envelope per the §20.4 contract.
    """
    relationships = relationships or []
    predicates_in_scope = set(predicate_filter) if predicate_filter else set(_KNOWN_PREDICATES)

    normalised = [
        _normalise(r, subject_entity_id)
        for r in relationships
    ]
    filtered = [
        r for r in normalised if r["predicate"] in predicates_in_scope
    ]
    # Sort by confidence DESC so the most-likely relationships
    # surface first for the answer-assembly step.
    filtered.sort(key=lambda r: r["confidence"], reverse=True)

    summary = (
        f"subject={subject_entity_id} relationships_in={len(relationships)} "
        f"after_filter={len(filtered)} "
        f"predicates_in_scope={len(predicates_in_scope)}"
    )
    logger.info("spatial_relationship: %s", summary)

    return {
        "workspace_id":     str(workspace_id),
        "project_id":       str(project_id),
        "subject_id":       subject_entity_id,
        "predicates_scope": sorted(predicates_in_scope),
        "relationships":    filtered,
        "summary":          summary,
        "queried_at":       datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["spatial_relationship"]
