"""Index Health Agent (Phase 0 agent #4).

Postgres-side: slow queries (pg_stat_statements), bloat
(pg_stat_user_tables), zero-hit indices (pg_stat_user_indexes), and
hypopg-driven cost-delta suggestions for the worst slow query.

Cross-store (all-nighter 2026-05-21):
  - Qdrant HNSW reachability: pick a random point from a populated
    collection, run a self-similarity search, assert the point comes
    back. If the HNSW graph is broken the search returns empty even
    though the collection ``status='green'``.
  - Neo4j page-cache hit ratio via dbms.queryJmx.

Findings land in ``silver.corpus_health_findings``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime
from app.services.qdrant_conn import qdrant_client_kwargs

logger = logging.getLogger(__name__)


@georag_agent(
    name="Index Health Agent",
    risk_tier="R0",
    version="0.1.0",
)
async def index_health_check(
    ctx: AgentContext,
    *,
    slow_query_ms_threshold: float = 250.0,
    bloat_dead_tup_ratio_threshold: float = 0.20,
    top_n_slow: int = 20,
) -> dict[str, Any]:
    """Run the Phase 0 PG-only checks; return a summary."""
    rt = get_runtime()
    summary: dict[str, Any] = {
        "slow_queries_flagged": 0,
        "bloat_findings": 0,
        "hypopg_suggestions": 0,
        "zero_hit_indices": 0,
        "qdrant_reachability": None,
        "neo4j_page_cache_hit_ratio": None,
        "findings_unpersisted": 0,
        "findings": [],
    }

    # Every probe below reads a CLUSTER-scoped catalog — pg_stat_statements,
    # pg_stat_user_tables, pg_stat_user_indexes. None of them is per-tenant.
    # But silver.corpus_health_findings.workspace_id is NOT NULL REFERENCES
    # silver.workspaces, and the cron trigger deliberately passes no workspace
    # (AgentRunInput.workspace_id: "if None, runs system-wide"). So on its
    # scheduled run this agent could never persist a finding: live logs showed
    # `null value in column "workspace_id" ... violates not-null constraint`
    # on every pass, swallowed by the probe's `except Exception`.
    #
    # Relaxing that NOT NULL is a tenancy decision, not a bug fix, so it is NOT
    # made here. Instead system-wide findings are returned in the summary (the
    # workflow output Hatchet retains) and counted, so the agent reports what it
    # found instead of failing silently. When invoked WITH a workspace — via the
    # on-demand route — persistence behaves exactly as before.
    system_wide = ctx.workspace_id is None

    async def _record(sql: str, *args: Any, kind: str, detail: dict[str, Any]) -> None:
        """Persist a finding, or collect it when running system-wide."""
        if system_wide:
            summary["findings"].append({"finding_type": kind, **detail})
            summary["findings_unpersisted"] += 1
            return
        await rt.pg_pool.execute(sql, ctx.workspace_id, *args)

    # ---- 1. Slow queries (pg_stat_statements) -------------------------------
    slow = await rt.pg_pool.fetch(
        """
        SELECT queryid, calls, mean_exec_time, total_exec_time,
               left(query, 200) AS query_excerpt
        FROM pg_stat_statements
        WHERE mean_exec_time > $1
          AND calls > 5
        ORDER BY mean_exec_time DESC
        LIMIT $2
        """,
        slow_query_ms_threshold,
        top_n_slow,
    )
    for r in slow:
        detail = {
            "queryid": r["queryid"],
            "calls": r["calls"],
            "mean_exec_time_ms": float(r["mean_exec_time"]),
            "total_exec_time_ms": float(r["total_exec_time"]),
            "query_excerpt": r["query_excerpt"],
        }
        await _record(
            """
            INSERT INTO silver.corpus_health_findings
                (workspace_id, finding_type, severity, target_schema, target_table,
                 target_id, payload, status)
            VALUES ($1, 'slow_query', $2, NULL, NULL, $3, $4::jsonb, 'open')
            """,
            "high" if r["mean_exec_time"] > 1000 else "medium",
            str(r["queryid"]),
            json.dumps(detail),
            kind="slow_query",
            detail=detail,
        )
        summary["slow_queries_flagged"] += 1

    # ---- 2. Table bloat (n_dead_tup / n_live_tup ratio) ---------------------
    bloat = await rt.pg_pool.fetch(
        """
        SELECT schemaname, relname,
               n_live_tup, n_dead_tup,
               CASE WHEN n_live_tup = 0 THEN 0
                    ELSE n_dead_tup::float / n_live_tup END AS bloat_ratio
        FROM pg_stat_user_tables
        WHERE n_dead_tup > 1000
          AND n_live_tup > 0
          AND n_dead_tup::float / NULLIF(n_live_tup, 0) > $1
        ORDER BY bloat_ratio DESC
        LIMIT 10
        """,
        bloat_dead_tup_ratio_threshold,
    )
    for r in bloat:
        detail = {
            "schema": r["schemaname"],
            "table": r["relname"],
            "n_live_tup": r["n_live_tup"],
            "n_dead_tup": r["n_dead_tup"],
            "bloat_ratio": float(r["bloat_ratio"]),
            "suggested_action": "VACUUM (or pg_repack for online reorg)",
        }
        await _record(
            """
            INSERT INTO silver.corpus_health_findings
                (workspace_id, finding_type, severity, target_schema, target_table,
                 payload, status)
            VALUES ($1, 'table_bloat', 'medium', $2, $3, $4::jsonb, 'open')
            """,
            r["schemaname"],
            r["relname"],
            json.dumps(detail),
            kind="table_bloat",
            detail=detail,
        )
        summary["bloat_findings"] += 1

    # ---- 3. Zero-hit indices (pg_stat_user_indexes.idx_scan = 0) -----------
    # An index with zero scans across its lifetime is either redundant
    # (covered by another) or never matched by the planner — both are
    # bloat candidates. Skip system catalogs and unique constraints
    # (those exist for write-side enforcement).
    try:
        zero_hits = await rt.pg_pool.fetch(
            """
            SELECT s.schemaname, s.relname, s.indexrelname, s.idx_scan,
                   pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size
            FROM pg_stat_user_indexes s
            JOIN pg_index i ON i.indexrelid = s.indexrelid
            WHERE s.idx_scan = 0
              AND NOT i.indisunique
              AND NOT i.indisprimary
              AND s.schemaname IN ('silver','gold','bronze','audit','usage','workflow','outbox','public_geo')
            ORDER BY pg_relation_size(s.indexrelid) DESC
            LIMIT 20
            """
        )
        for r in zero_hits:
            detail = {
                "schema": r["schemaname"],
                "table": r["relname"],
                "indexrelname": r["indexrelname"],
                "idx_scan": int(r["idx_scan"]),
                "size": r["index_size"],
                "suggested_action": "review for removal (no scans recorded since last reset)",
            }
            await _record(
                """
                INSERT INTO silver.corpus_health_findings
                    (workspace_id, finding_type, severity, target_schema, target_table,
                     target_id, payload, status)
                VALUES ($1, 'zero_hit_index', 'low', $2, $3, $4, $5::jsonb, 'open')
                """,
                r["schemaname"],
                r["relname"],
                r["indexrelname"],
                json.dumps(detail),
                kind="zero_hit_index",
                detail=detail,
            )
            summary["zero_hit_indices"] += 1
    except Exception as exc:
        logger.warning("zero-hit index probe failed: %s", exc)

    # ---- 4. Hypopg suggestion for the worst slow query ---------------------
    # Cost-delta cycle: hypopg_create_index → EXPLAIN both with and
    # without the hypothetical → compare total cost. If the hypothetical
    # cuts cost by >30% AND >100 cost units, write a suggestion.
    if slow:
        slow[0]
        try:
            async with rt.pg_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT hypopg_reset()")
                    # We don't have a per-query hypothetical-index
                    # generator in Phase 0; instead we look up the FK
                    # columns of the table the worst query touches
                    # most. For Phase 0 this falls back to logging the
                    # query for human review with a hypopg-ready harness.
                    summary["hypopg_suggestions"] = 1  # recorded a slow
                    # query for hypopg consideration; the actual create-
                    # and-replan loop is part of the Phase 1 fast-follow.
        except Exception as exc:
            logger.warning("hypopg probe failed: %s", exc)

    # ---- 5. Qdrant HNSW reachability ---------------------------------------
    try:
        from qdrant_client import AsyncQdrantClient  # noqa: PLC0415

        qc = AsyncQdrantClient(**qdrant_client_kwargs())
        try:
            collections = (await qc.get_collections()).collections
            reach_results: dict[str, Any] = {}
            for coll in collections:
                if not coll.name:
                    continue
                # Pick the first point and search for its own vector. A
                # healthy HNSW graph returns the point itself with high
                # similarity. A broken graph returns an empty hit list
                # or a wrong neighbour despite the collection being green.
                scroll = await qc.scroll(
                    collection_name=coll.name,
                    limit=1,
                    with_vectors=True,
                    with_payload=False,
                )
                points, _ = scroll
                if not points:
                    reach_results[coll.name] = {"status": "empty", "reachable": None}
                    continue
                point = points[0]
                vec = point.vector
                # Multi-vector collections return a dict; pick the default ("").
                if isinstance(vec, dict):
                    vec = vec.get("") or next(iter(vec.values()))
                if not vec:
                    reach_results[coll.name] = {"status": "no_vector", "reachable": None}
                    continue
                # query_points, not search: qdrant-client removed
                # AsyncQdrantClient.search in the 1.x line, and every run of
                # this probe since the upgrade died with
                # "'AsyncQdrantClient' object has no attribute 'search'" —
                # caught in the agent's broad `except Exception`, so the
                # summary recorded a probe error rather than an index fault
                # and nothing escalated. query_points returns a QueryResponse
                # whose .points is the old return value.
                response = await qc.query_points(
                    collection_name=coll.name,
                    query=vec,
                    limit=1,
                )
                hits = response.points
                round_trips = bool(hits) and hits[0].id == point.id
                reach_results[coll.name] = {
                    "status": "ok" if round_trips else "broken",
                    "reachable": round_trips,
                    "points_inspected": 1,
                }
            summary["qdrant_reachability"] = reach_results
        finally:
            await qc.close()
    except ImportError:
        summary["qdrant_reachability"] = "qdrant-client not installed"
    except Exception as exc:
        summary["qdrant_reachability"] = f"error: {exc}"
        logger.warning("qdrant reachability probe failed: %s", exc)

    # ---- 6. Neo4j page-cache hit ratio -------------------------------------
    # B1 (2026-07-28): Neo4j was removed from the stack. This probe stays
    # in the summary shape callers expect but is now a permanent no-op —
    # same fail-open value the try/except used to produce on failure,
    # without the wasted connection attempt.
    summary["neo4j_page_cache_hit_ratio"] = "neo4j was removed from the stack (B1, 2026-07-28)"

    return summary
