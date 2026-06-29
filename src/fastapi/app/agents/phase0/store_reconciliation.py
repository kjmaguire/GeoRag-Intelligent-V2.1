"""Store Reconciliation Agent (Phase 0 agent #5).

Phase 0 scope: outbox-only reconciliation. Phase 1 scope (added in the
all-nighter 2026-05-21): cross-store count diffs against Qdrant and
Neo4j to detect drift the outbox missed. Surfaces:

  - dead_lettered propagations (status='dead_lettered' or dead_lettered_at IS NOT NULL)
  - stuck propagations (status='in_flight' for > 1 hour)
  - missing_in_b candidates (pending_propagations row > 1 hour old without
    any successful propagation_attempts row)
  - cross_store_drift: workspace-scoped count diffs between
    silver.document_passages and Qdrant georag_reports points;
    silver.projects vs Neo4j (:Project) nodes. Drift > 5% (or >10 abs)
    becomes a finding. Clients are lazily-imported and a missing client
    is a no-op, not an error.

All findings write to ``silver.store_reconciliation_findings``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime

logger = logging.getLogger(__name__)


@georag_agent(
    name="Store Reconciliation Agent",
    risk_tier="R0",
    version="0.1.0",
)
async def store_reconciliation_run(
    ctx: AgentContext,
    *,
    stuck_threshold_minutes: int = 60,
    missing_threshold_minutes: int = 60,
) -> dict[str, Any]:
    """Surface outbox-side drift; cross-store checks deferred to Phase 1."""
    rt = get_runtime()
    summary: dict[str, Any] = {
        "dead_lettered": 0,
        "stuck": 0,
        "missing_in_b": 0,
        "cross_store_drift": {},
    }

    # ---- 1. Dead-lettered propagations -------------------------------------
    dead = await rt.pg_pool.fetch(
        """
        SELECT id, workspace_id, source_schema, source_table, source_id,
               target_store, target_collection, dead_lettered_at
        FROM outbox.pending_propagations
        WHERE status = 'dead_lettered'
          AND dead_lettered_at >= now() - interval '7 days'
        """
    )
    for r in dead:
        await rt.pg_pool.execute(
            """
            INSERT INTO silver.store_reconciliation_findings
                (workspace_id, drift_type, severity, source_store, target_store,
                 source_id, details, discovered_by)
            VALUES ($1, 'outbox_dead_letter', 'high', 'postgres', $2,
                    $3, $4::jsonb, 'Store Reconciliation Agent')
            """,
            r["workspace_id"],
            r["target_store"],
            r["source_id"],
            json.dumps(
                {
                    "propagation_id": str(r["id"]),
                    "source": f"{r['source_schema']}.{r['source_table']}",
                    "target_collection": r["target_collection"],
                    "dead_lettered_at": r["dead_lettered_at"].isoformat() if r["dead_lettered_at"] else None,
                }
            ),
        )
        summary["dead_lettered"] += 1

    # ---- 2. Stuck propagations (in_flight > N minutes) ---------------------
    stuck = await rt.pg_pool.fetch(
        """
        SELECT id, workspace_id, source_schema, source_table, source_id,
               target_store, last_attempted_at
        FROM outbox.pending_propagations
        WHERE status = 'in_flight'
          AND (last_attempted_at IS NULL
               OR last_attempted_at < now() - ($1 || ' minutes')::interval)
        """,
        str(stuck_threshold_minutes),
    )
    for r in stuck:
        await rt.pg_pool.execute(
            """
            INSERT INTO silver.store_reconciliation_findings
                (workspace_id, drift_type, severity, source_store, target_store,
                 source_id, details, discovered_by)
            VALUES ($1, 'stuck_propagation', 'medium', 'postgres', $2,
                    $3, $4::jsonb, 'Store Reconciliation Agent')
            """,
            r["workspace_id"],
            r["target_store"],
            r["source_id"],
            json.dumps(
                {
                    "propagation_id": str(r["id"]),
                    "stuck_minutes": stuck_threshold_minutes,
                    "last_attempted_at": r["last_attempted_at"].isoformat() if r["last_attempted_at"] else None,
                }
            ),
        )
        summary["stuck"] += 1

    # ---- 3. Pending without any attempt > N minutes (dispatcher missed) ---
    missing = await rt.pg_pool.fetch(
        """
        SELECT p.id, p.workspace_id, p.source_schema, p.source_table, p.source_id,
               p.target_store, p.enqueued_at
        FROM outbox.pending_propagations p
        WHERE p.status = 'pending'
          AND p.enqueued_at < now() - ($1 || ' minutes')::interval
          AND NOT EXISTS (
              SELECT 1 FROM outbox.propagation_attempts a WHERE a.propagation_id = p.id
          )
        """,
        str(missing_threshold_minutes),
    )
    for r in missing:
        await rt.pg_pool.execute(
            """
            INSERT INTO silver.store_reconciliation_findings
                (workspace_id, drift_type, severity, source_store, target_store,
                 source_id, details, discovered_by)
            VALUES ($1, 'missing_in_b', 'medium', 'postgres', $2,
                    $3, $4::jsonb, 'Store Reconciliation Agent')
            """,
            r["workspace_id"],
            r["target_store"],
            r["source_id"],
            json.dumps(
                {
                    "propagation_id": str(r["id"]),
                    "enqueued_at": r["enqueued_at"].isoformat(),
                    "note": "no propagation_attempts row exists — dispatcher likely missed it",
                }
            ),
        )
        summary["missing_in_b"] += 1

    # ---- 4. Cross-store count diffs (all-nighter 2026-05-21) --------------
    # PG → Qdrant: workspace-scoped passage count must match the
    # collection's points_count for that workspace. PG → Neo4j: project
    # row count must match (:Project {workspace_id:$1}) node count.
    # 5% relative or 10 absolute = drift finding.
    try:
        pg_passages = await rt.pg_pool.fetchval(
            "SELECT count(*) FROM silver.document_passages WHERE workspace_id = $1",
            ctx.workspace_id,
        ) or 0
        pg_projects = await rt.pg_pool.fetchval(
            "SELECT count(*) FROM silver.projects WHERE workspace_id = $1",
            ctx.workspace_id,
        ) or 0
    except Exception as exc:
        logger.warning("cross_store_drift: pg counts failed: %s", exc)
        pg_passages = pg_projects = None

    qdrant_count = None
    try:
        from qdrant_client import AsyncQdrantClient  # noqa: PLC0415

        qc = AsyncQdrantClient(
            host=os.environ.get("QDRANT_HOST", "qdrant"),
            port=int(os.environ.get("QDRANT_PORT", "6333")),
        )
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue  # noqa: PLC0415

            # ADR-0010: canonical collection is georag_chunks when
            # RETRIEVAL_USE_DOCUMENT_PASSAGES is true (the default since
            # 2026-05-28). Hardcoded "georag_reports" reported false drift
            # post-cutover because passages now land in chunks. Use the
            # same flag the live retrieval path consults.
            from app.config import settings  # local import to avoid cycle
            _collection = (
                "georag_chunks"
                if settings.RETRIEVAL_USE_DOCUMENT_PASSAGES
                else "georag_reports"
            )
            r = await qc.count(
                collection_name=_collection,
                count_filter=Filter(must=[
                    FieldCondition(
                        key="workspace_id",
                        match=MatchValue(value=str(ctx.workspace_id)),
                    )
                ]),
                exact=True,
            )
            qdrant_count = r.count
        finally:
            await qc.close()
    except ImportError:
        logger.info("cross_store_drift: qdrant-client not installed — skip")
    except Exception as exc:
        logger.warning("cross_store_drift: qdrant query failed: %s", exc)

    neo4j_count = None
    try:
        from neo4j import AsyncGraphDatabase  # noqa: PLC0415

        driver = AsyncGraphDatabase.driver(
            os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
            auth=(
                os.environ.get("NEO4J_USERNAME", "neo4j"),
                os.environ.get("NEO4J_PASSWORD", ""),
            ),
        )
        try:
            async with driver.session() as session:
                res = await session.run(
                    "MATCH (n:Project {workspace_id: $ws}) RETURN count(n) AS c",
                    ws=str(ctx.workspace_id),
                )
                rec = await res.single()
                neo4j_count = rec["c"] if rec else 0
        finally:
            await driver.close()
    except ImportError:
        logger.info("cross_store_drift: neo4j driver not installed — skip")
    except Exception as exc:
        logger.warning("cross_store_drift: neo4j query failed: %s", exc)

    def _drift_finding(pg: int | None, store: int | None) -> dict[str, Any]:
        if pg is None or store is None:
            return {"pg": pg, "store": store, "drift": None}
        abs_diff = abs(pg - store)
        rel = abs_diff / max(pg, 1)
        return {
            "pg": pg,
            "store": store,
            "abs_diff": abs_diff,
            "rel_drift": round(rel, 4),
            "is_drift": abs_diff > 10 and rel > 0.05,
        }

    summary["cross_store_drift"] = {
        "qdrant_georag_reports": _drift_finding(pg_passages, qdrant_count),
        "neo4j_project_nodes": _drift_finding(pg_projects, neo4j_count),
    }

    for store_name, finding in summary["cross_store_drift"].items():
        if finding.get("is_drift"):
            try:
                await rt.pg_pool.execute(
                    """
                    INSERT INTO silver.store_reconciliation_findings
                        (workspace_id, drift_type, severity, source_store, target_store,
                         source_id, details, discovered_by)
                    VALUES ($1, 'cross_store_drift', 'high', 'postgres', $2,
                            $3, $4::jsonb, 'Store Reconciliation Agent')
                    """,
                    ctx.workspace_id,
                    store_name,
                    store_name,
                    json.dumps(finding),
                )
            except Exception as exc:
                logger.warning("cross_store_drift insert failed: %s", exc)

    return summary
