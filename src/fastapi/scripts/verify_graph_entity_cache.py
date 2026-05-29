"""
Smoke-test / dev verification for the graph entity cache layer.

Tests the fetch_project_graph_entities cold/warm path, Redis TTL,
Redis-down and Neo4j-down graceful degradation, and entity extraction
logic against a known real project UUID in the dev database.

Usage (run from src/fastapi/ with the dev stack up):
    uv run python scripts/verify_graph_entity_cache.py

Requires: REAL project UUID below to exist in Neo4j. The EMPTY UUID is
synthetic and tests the universal-entity fallback path.

Note: REAL is the Athabasca Group project seeded during Module 1 dev-data
load. If the dev database has been wiped, update this UUID before running.
"""
import asyncio
import time

from neo4j import AsyncGraphDatabase
import redis.asyncio as aioredis

from app.config import settings
from app.agent.orchestrator import (
    fetch_project_graph_entities,
    _extract_graph_entities,
    _UNIVERSAL_GRAPH_ENTITIES,
)

# Real project UUID from dev seed data (Athabasca Group project).
REAL = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
# Synthetic UUID for empty-project / universal-fallback test.
EMPTY = '00000000-0000-0000-0000-000000000001'


async def main() -> None:
    drv = AsyncGraphDatabase.driver(
        f'bolt://{settings.NEO4J_HOST}:{settings.NEO4J_PORT}',
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    rc = aioredis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
    )

    # Clear cache to measure cold path
    await rc.delete(f'georag:graph_entities:v1:{REAL}')

    t = time.perf_counter()
    cold = await fetch_project_graph_entities(project_id=REAL, neo4j_driver=drv, redis_client=rc)
    dt_cold = (time.perf_counter() - t) * 1000
    print(f'cold fetch (real proj): count={len(cold)} dt={dt_cold:.1f}ms top5={cold[:5]}')

    t = time.perf_counter()
    warm = await fetch_project_graph_entities(project_id=REAL, neo4j_driver=drv, redis_client=rc)
    dt_warm = (time.perf_counter() - t) * 1000
    print(f'warm fetch (cache hit): count={len(warm)} dt={dt_warm:.1f}ms speedup={dt_cold / dt_warm:.1f}x')

    assert cold == warm, 'cache should return identical result'
    assert all(u in cold for u in _UNIVERSAL_GRAPH_ENTITIES), 'universal codes always present'

    # Empty project — should degrade gracefully to just universals.
    empty = await fetch_project_graph_entities(project_id=EMPTY, neo4j_driver=drv, redis_client=rc)
    print(f'empty project: count={len(empty)} (expected 4 universals only) list={empty}')
    assert empty == _UNIVERSAL_GRAPH_ENTITIES, f'expected only universals, got {empty}'

    # Redis-down simulation — pass None client; should still hit Neo4j.
    no_redis = await fetch_project_graph_entities(project_id=REAL, neo4j_driver=drv, redis_client=None)
    print(f'no-redis mode: count={len(no_redis)}')
    assert len(no_redis) >= len(_UNIVERSAL_GRAPH_ENTITIES)

    # Neo4j-down simulation — pass a broken driver; should return universals.
    class BrokenDriver:
        def session(self, *a, **kw):
            class _S:
                async def __aenter__(self_):
                    raise RuntimeError('neo4j down')

                async def __aexit__(self_, *a):
                    pass

            return _S()

    broken = await fetch_project_graph_entities(
        project_id=REAL, neo4j_driver=BrokenDriver(), redis_client=None
    )
    print(f'neo4j-down: count={len(broken)} list={broken} (expected universals only)')
    assert broken == _UNIVERSAL_GRAPH_ENTITIES

    # Extraction logic — must be pure and preserve input order.
    matches = _extract_graph_entities('Tell me about Triple R and Athabasca Group', cold)
    print(f'extract: matches={matches}')
    assert any('Triple R' in m for m in matches)
    assert any('Athabasca' in m for m in matches)

    # Redis schema + TTL
    ttl = await rc.ttl(f'georag:graph_entities:v1:{REAL}')
    print(f'cache TTL: {ttl}s (expected ~900)')
    assert 0 < ttl <= 900

    await drv.close()
    await rc.aclose()
    print('ALL GRAPH CHECKS PASSED')


asyncio.run(main())
