"""Project-scoped graph-entity fetch (Neo4j + Redis).

Extracted from ``app.agent.orchestrator`` in Phase F.8 (see
``docs/master_plan_orchestrator_refactor.md``). The orchestrator
re-exports the public surface so callers that import
``from app.agent.orchestrator import fetch_project_graph_entities``
keep working unchanged.

Module contents:

* ``_UNIVERSAL_GRAPH_ENTITIES`` — always-match lithology codes that
  appear across every project.
* ``fetch_project_graph_entities`` — async fetch of the project's
  top-N entities by in-degree, with 15-min Redis caching and graceful
  degradation when Neo4j is empty / unreachable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


# Always-match lithology codes. These are 3-4 letter geological symbols that
# appear in queries across all projects and are rare enough in English that
# false-positives are acceptable. Project-specific entities (deposit names,
# formation names, QP names) come from Neo4j via fetch_project_graph_entities.
_UNIVERSAL_GRAPH_ENTITIES: list[str] = ["SST", "CGL", "PGN", "GPT"]


async def fetch_project_graph_entities(
    project_id: str,
    neo4j_driver: Any,
    redis_client: Any | None = None,
    limit: int = 50,
) -> list[str]:
    """Return the top-N named entities in this project's subgraph, by in-degree.

    Replaces the previous hardcoded ``_KNOWN_GRAPH_ENTITIES`` list which was
    scoped to one project (Lazy Edward Bay). Cached in Redis for 15 min so
    the per-request cost is one GET on the warm path. On cold path the
    Neo4j round-trip is bounded by ``settings.TIMEOUT_NEO4J_S``.

    On any failure (Redis down, Neo4j timeout, empty graph) the function
    returns the universal lithology codes so the classifier still produces
    something — the graph branch degrades gracefully rather than failing.
    """
    cache_key = f"georag:graph_entities:v1:{project_id}"

    # ── Redis cache lookup ────────────────────────────────────────────────
    if redis_client is not None:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                import json as _json
                names = _json.loads(cached)
                if isinstance(names, list):
                    return list(names) + _UNIVERSAL_GRAPH_ENTITIES
        except Exception:
            logger.debug("fetch_project_graph_entities: redis read failed", exc_info=True)

    # ── Neo4j query ───────────────────────────────────────────────────────
    # Rank by in-degree. Entities with many relationships are the ones the
    # user is most likely referring to when they say "the deposit" or "the
    # formation". Limit is a safeguard against very dense graphs.
    # Neo4j 2026: length() only accepts PATH; use size() on strings/lists.
    # Secondary sort by name length (descending) so longer/more-specific
    # names are tried first by the substring matcher — "Triple R Deposit"
    # before "Triple R" before "Deposit".
    #
    # Doc-phase 188 (Phase F.3) — INVESTIGATED, fully REVERTED.
    # Hypothesis: 1,100+ Report nodes from OCR ingest were pushing
    # Formation/Deposit entities past the limit cutoff. Tested two fixes:
    #   - Report/Publication exclusion: 6/10 → 5/10 (regression — Report
    #     title tokens were contributing to entity-grounding for location
    #     queries; removing them hurt "What county and state" which had
    #     previously been passing).
    #   - Limit bump (50 → 200): also 6/10 → 5/10 (more entities in
    #     prompt diluted the entity-grounding signal).
    # Conclusion: the current entity-resolution surface is well-tuned
    # for the existing eval question set. Reports ARE useful even as
    # document references. The real fix for the deposit-type question
    # is structured-tool wiring (Phase F.4), not entity-list shaping.
    cypher = (
        "MATCH (n) "
        "WHERE n.project_id = $project_id AND n.name IS NOT NULL "
        "OPTIONAL MATCH (n)-[r]-() "
        "WITH n.name AS name, count(r) AS degree "
        "WHERE degree >= 1 "
        "RETURN DISTINCT name, degree "
        "ORDER BY degree DESC, size(name) DESC "
        "LIMIT $limit"
    )

    names: list[str] = []
    try:
        async def _run() -> list[str]:
            async with neo4j_driver.session() as session:
                result = await session.run(cypher, project_id=project_id, limit=limit)
                records = await result.data()
            return [str(r["name"]) for r in records if r.get("name")]

        names = await asyncio.wait_for(_run(), timeout=settings.TIMEOUT_NEO4J_S)
    except TimeoutError:
        logger.warning(
            "fetch_project_graph_entities: timed out after %.1fs project=%s",
            settings.TIMEOUT_NEO4J_S,
            project_id,
        )
    except Exception:
        logger.exception("fetch_project_graph_entities: neo4j query failed project=%s", project_id)

    # ── Redis cache write (15 min TTL) ────────────────────────────────────
    if redis_client is not None and names:
        try:
            import json as _json
            await redis_client.setex(cache_key, 900, _json.dumps(names))
        except Exception:
            logger.debug("fetch_project_graph_entities: redis write failed", exc_info=True)

    return names + _UNIVERSAL_GRAPH_ENTITIES


__all__ = [
    "_UNIVERSAL_GRAPH_ENTITIES",
    "fetch_project_graph_entities",
]
