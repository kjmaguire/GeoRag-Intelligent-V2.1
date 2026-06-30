"""Layer 4 — Entity Resolution.

Architecture reference: Section 04i, Layer 4.

Purpose
-------
Verify that any drill-hole IDs, formation names, or project names mentioned in
the response text actually exist in the data stores for the current project.
Prevents the LLM from hallucinating plausible-sounding but non-existent hole
IDs or deposit names.

Two entity classes are verified:
  1. Drill-hole IDs — matched against ``silver.collars`` via PostGIS.
     Pattern: one or more uppercase letters, a hyphen, one or more digits,
     optionally another hyphen + digits.  Examples: PLS-20-01, ATDD-001, RC-42.

  2. Quoted names — any text wrapped in double or single quotes in the response.
     These are checked against Neo4j node names scoped to the project.
     If Neo4j has no data yet (Milestone 3) the check is skipped gracefully.

Design decisions
----------------
- Drill-hole ID resolution is authoritative: if the ID looks like a hole ID
  pattern and does not exist in silver.collars for this project, it is an
  error.
- Quoted-name resolution is best-effort: if Neo4j returns no results we skip
  rather than rejecting, because the graph may not be fully populated.
- The validator is disabled when settings.ENTITY_RESOLUTION_ENABLED is False.

Pydantic AI output_validator
-----------------------------
Registered in geo_agent.py with ``@geo_agent.output_validator``.
"""

from __future__ import annotations

import asyncio
import logging
import re

from pydantic_ai import ModelRetry, RunContext

from app.agent.deps import AgentDeps
from app.config import settings
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

# Drill-hole ID pattern: uppercase prefix, year segment, sequence segment.
# Requires TWO hyphen-separated digit groups to avoid matching citation markers
# like [DATA-1] or [PUB-3]. Real drill holes always have year + sequence.
# Examples: PLS-20-01, ATDD-001-05, RC-42-03
_HOLE_ID_RE = re.compile(r"\b([A-Z]{1,8}-\d{1,6}-\d{1,6})\b")

# Citation markers — stripped before drill-hole extraction to avoid false positives.
_CITATION_MARKER_RE = re.compile(r"\[(?:DATA|NI43|PUB)-\d+\]")

# Blocklist for known prefixes that are citation types, not hole ID prefixes.
_CITATION_PREFIXES: frozenset[str] = frozenset({"DATA", "NI43", "PUB"})

# Anything in double or single quotes (non-greedy, single-line).
_QUOTED_NAME_RE = re.compile(r'["\']([^"\']{2,80})["\']')


def _extract_hole_ids(text: str) -> list[str]:
    """Extract potential drill-hole IDs from response text."""
    # Strip citation markers first so digits inside them can't form hole IDs.
    clean = _CITATION_MARKER_RE.sub("", text)
    candidates = _HOLE_ID_RE.findall(clean)
    # Filter out any remaining matches whose prefix is a citation type.
    filtered = [hid for hid in candidates if hid.split("-", 1)[0] not in _CITATION_PREFIXES]
    return list(dict.fromkeys(filtered))  # deduplicate, preserve order


# UUIDs (any case), all-lowercase identifiers (snake_case field names like
# "source_chunk_id"), and all-uppercase enum values ("DATA", "NI43") should
# not be treated as quoted entity names.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SCREAMING_CASE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_structural_noise(name: str) -> bool:
    """True if a quoted string is clearly a JSON field name, UUID, or enum."""
    if _UUID_RE.match(name):
        return True
    if _SNAKE_CASE_RE.match(name):  # source_chunk_id, citation_type, etc.
        return True
    if _SCREAMING_CASE_RE.match(name):  # DATA, NI43, PUB
        return True
    return False


def _extract_quoted_names(text: str) -> list[str]:
    """Extract quoted entity names from response text.

    Filters out JSON field names, UUIDs, and enum constants that are not
    real entity references. Only returns human-meaningful proper names.
    """
    all_matches = _QUOTED_NAME_RE.findall(text)
    filtered = [name for name in all_matches if not _is_structural_noise(name)]
    return list(dict.fromkeys(filtered))


async def _resolve_hole_ids_in_postgres(
    hole_ids: list[str],
    project_id: str,
    pg_pool: object,
) -> list[str]:
    """Return the subset of hole_ids that do NOT exist in silver.collars.

    Returns an empty list if all IDs are found, or if the database is
    unavailable (we fail open rather than blocking the response).
    """
    if not hole_ids:
        return []

    # Build a parameterised ANY query: WHERE hole_id = ANY($1) AND project_id = $2
    sql = (
        "SELECT hole_id FROM silver.collars "
        "WHERE hole_id = ANY($1) AND project_id = $2::uuid"
    )

    async def _run() -> list[str]:
        async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(sql, hole_ids, project_id)
            return [row["hole_id"] for row in rows]

    try:
        found_ids = await asyncio.wait_for(_run(), timeout=settings.TIMEOUT_POSTGIS_S)
    except TimeoutError:
        logger.warning(
            "layer4_entity: hole-ID resolution timed out for project=%s — "
            "skipping entity check (fail open)",
            project_id,
        )
        return []
    except Exception:
        logger.exception(
            "layer4_entity: hole-ID resolution failed for project=%s — "
            "skipping entity check (fail open)",
            project_id,
        )
        return []

    found_set = set(found_ids)
    missing = [hid for hid in hole_ids if hid not in found_set]
    return missing


async def _resolve_names_in_neo4j(
    names: list[str],
    project_id: str,
    neo4j_driver: object,
) -> list[str]:
    """Return the subset of names that do NOT exist as Neo4j nodes for this project.

    Returns an empty list (no unknowns) if Neo4j is unavailable or empty —
    we fail open because the graph may not be populated yet (pre-Milestone 3).
    """
    if not names:
        return []

    cypher = (
        "UNWIND $names AS name "
        "OPTIONAL MATCH (n {project_id: $project_id}) "
        "  WHERE toLower(n.name) = toLower(name) "
        "RETURN name, n.name AS resolved"
    )

    async def _run() -> list[str]:
        async with neo4j_driver.session() as session:  # type: ignore[union-attr]
            result = await session.run(cypher, names=names, project_id=project_id)
            records = await result.data()
        unresolved = [
            rec["name"] for rec in records if rec.get("resolved") is None
        ]
        return unresolved

    try:
        unresolved = await asyncio.wait_for(_run(), timeout=settings.TIMEOUT_NEO4J_S)
    except TimeoutError:
        logger.warning(
            "layer4_entity: Neo4j name resolution timed out for project=%s — "
            "skipping (fail open)",
            project_id,
        )
        return []
    except Exception:
        logger.debug(
            "layer4_entity: Neo4j name resolution unavailable for project=%s — "
            "skipping (fail open, graph may not be populated yet)",
            project_id,
        )
        return []

    return unresolved


async def resolve_entity_references(
    ctx: RunContext[AgentDeps],
    output: GeoRAGResponse,
) -> GeoRAGResponse:
    """Output validator: verify drill-hole IDs and quoted names in the response.

    This is hallucination prevention Layer 4.

    Registration: @geo_agent.output_validator in geo_agent.py.

    Raises:
        ModelRetry: if any drill-hole ID does not exist in silver.collars for
            the current project, or if any quoted entity name cannot be
            resolved in the Neo4j graph (when graph data is available).

    Returns:
        The unchanged output if all entities resolve successfully.
    """
    if not settings.ENTITY_RESOLUTION_ENABLED:
        logger.debug("layer4_entity: disabled via settings — skipping")
        return output

    project_id = ctx.deps.project_id
    text = output.text

    hole_ids = _extract_hole_ids(text)
    quoted_names = _extract_quoted_names(text)

    logger.debug(
        "layer4_entity: found %d hole IDs and %d quoted names in response text",
        len(hole_ids),
        len(quoted_names),
    )

    # Run both resolution checks concurrently.
    missing_holes, unresolved_names = await asyncio.gather(
        _resolve_hole_ids_in_postgres(hole_ids, project_id, ctx.deps.pg_pool),
        _resolve_names_in_neo4j(quoted_names, project_id, ctx.deps.neo4j_driver),
    )

    problems: list[str] = []

    if missing_holes:
        logger.warning(
            "layer4_entity: %d hole ID(s) not found in silver.collars "
            "for project=%s: %s",
            len(missing_holes),
            project_id,
            ", ".join(missing_holes),
        )
        problems.append(
            f"Drill-hole ID(s) not found in the database for this project: "
            f"{', '.join(missing_holes)}. "
            f"Only use hole IDs that were returned by tool_query_spatial_collars."
        )

    if unresolved_names:
        logger.warning(
            "layer4_entity: %d quoted name(s) not resolved in Neo4j "
            "for project=%s: %s",
            len(unresolved_names),
            project_id,
            ", ".join(unresolved_names),
        )
        problems.append(
            f"Quoted entity name(s) could not be resolved in the knowledge graph: "
            f"{', '.join(repr(n) for n in unresolved_names)}. "
            f"Use entity names exactly as returned by tool_traverse_knowledge_graph, "
            f"or omit the name if it did not appear in any tool result."
        )

    if problems:
        raise ModelRetry(
            "Entity resolution failed (hallucination prevention Layer 4):\n\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n\nRewrite your response using only entity names that appeared "
            "in tool call results."
        )

    logger.debug(
        "layer4_entity: all entities resolved — %d hole IDs, %d quoted names",
        len(hole_ids),
        len(quoted_names),
    )
    return output
