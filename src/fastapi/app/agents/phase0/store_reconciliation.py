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

All findings write to ``silver.store_reconciliation_findings``, whose
``workspace_id`` is NOT NULL, so a propagation with no workspace (the outbox
column is nullable) cannot be recorded there; those are counted in
``unscoped_skipped`` instead of crashing the run.

The cross-store check needs one workspace at a time. Called with a workspace it
compares that one; called without (the nightly cron) it enumerates
``silver.workspaces`` and compares each with its scope bound. It used to count
``WHERE workspace_id = NULL`` in Postgres and filter Qdrant on the string
``"None"``, so the cron compared zero to zero every night.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime
from app.db import list_workspace_ids, scoped_connection
from app.services.qdrant_conn import qdrant_client_kwargs

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
        "unscoped_skipped": 0,
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
        if r["workspace_id"] is None:
            summary["unscoped_skipped"] += 1
            continue
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
        if r["workspace_id"] is None:
            summary["unscoped_skipped"] += 1
            continue
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
        if r["workspace_id"] is None:
            summary["unscoped_skipped"] += 1
            continue
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

    if summary["unscoped_skipped"]:
        logger.warning(
            "store_reconciliation: %d propagation(s) have no workspace_id and cannot "
            "be recorded as findings (silver.store_reconciliation_findings.workspace_id "
            "is NOT NULL)",
            summary["unscoped_skipped"],
        )

    # ---- 4. Cross-store count diffs (all-nighter 2026-05-21) --------------
    # PG → Qdrant: workspace-scoped passage count must match the
    # collection's points_count for that workspace. PG → Neo4j: project
    # row count must match (:Project {workspace_id:$1}) node count.
    # 5% relative or 10 absolute = drift finding.
    #
    # One workspace at a time. With no workspace in scope (the nightly cron)
    # every workspace is compared in turn: the Postgres counts run with that
    # workspace's scope bound, because a strict RLS policy reads zero rows
    # without it (app/db/workspace_sweep.py), and Qdrant is filtered on its
    # id. Passing None into those filters is what made this compare 0 to 0.
    from app.config import settings  # noqa: PLC0415 — local import to avoid cycle

    # ADR-0010: canonical collection is georag_chunks when
    # RETRIEVAL_USE_DOCUMENT_PASSAGES is true (the default since 2026-05-28).
    # Hardcoded "georag_reports" reported false drift post-cutover because
    # passages now land in chunks. Use the same flag the live retrieval path
    # consults. Computed unconditionally so the summary key below is always
    # defined, even when qdrant-client is missing or the query fails.
    _collection = (
        "georag_chunks"
        if settings.RETRIEVAL_USE_DOCUMENT_PASSAGES
        else "georag_reports"
    )

    # B1 (2026-07-28): Neo4j was removed from the stack. neo4j_count stays
    # None (the same fail-open value the try/except used to produce when
    # the driver was unreachable) so the drift finding below degrades to
    # "no comparison possible" instead of attempting a doomed connection.
    neo4j_count = None

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

    workspace_ids: list[str]
    if ctx.workspace_id is not None:
        workspace_ids = [str(ctx.workspace_id)]
    else:
        try:
            async with rt.pg_pool.acquire() as conn:
                workspace_ids = await list_workspace_ids(
                    conn, site="phase0.store_reconciliation",
                )
        except Exception as exc:
            logger.warning("cross_store_drift: workspace enumeration failed: %s", exc)
            workspace_ids = []
            summary["cross_store_skipped"] = (
                f"workspace enumeration failed: {type(exc).__name__}"
            )
        else:
            if not workspace_ids:
                summary["cross_store_skipped"] = "no workspaces to compare"

    qc = None
    if workspace_ids:
        try:
            from qdrant_client import AsyncQdrantClient  # noqa: PLC0415

            qc = AsyncQdrantClient(**qdrant_client_kwargs())
        except ImportError:
            logger.info("cross_store_drift: qdrant-client not installed — skip")
        except Exception as exc:
            logger.warning("cross_store_drift: qdrant client failed: %s", exc)

    try:
        for workspace_id in workspace_ids:
            try:
                async with scoped_connection(
                    rt.pg_pool, workspace_id=workspace_id, site="phase0.store_reconciliation",
                ) as conn:
                    pg_passages = await conn.fetchval(
                        "SELECT count(*) FROM silver.document_passages WHERE workspace_id = $1::uuid",
                        workspace_id,
                    ) or 0
                    pg_projects = await conn.fetchval(
                        "SELECT count(*) FROM silver.projects WHERE workspace_id = $1::uuid",
                        workspace_id,
                    ) or 0
            except Exception as exc:
                logger.warning("cross_store_drift: pg counts failed for %s: %s", workspace_id, exc)
                pg_passages = pg_projects = None

            qdrant_count = None
            if qc is not None:
                try:
                    from qdrant_client.models import FieldCondition, Filter, MatchValue  # noqa: PLC0415

                    r = await qc.count(
                        collection_name=_collection,
                        count_filter=Filter(must=[
                            FieldCondition(
                                key="workspace_id",
                                match=MatchValue(value=workspace_id),
                            )
                        ]),
                        exact=True,
                    )
                    qdrant_count = r.count
                except Exception as exc:
                    logger.warning(
                        "cross_store_drift: qdrant query failed for %s: %s", workspace_id, exc,
                    )

            findings = {
                # Key name follows _collection above — the counting logic already
                # targets georag_chunks post-ADR-0010; the label used to still say
                # "georag_reports" even when the count was against georag_chunks.
                f"qdrant_{_collection}": _drift_finding(pg_passages, qdrant_count),
                "neo4j_project_nodes": _drift_finding(pg_projects, neo4j_count),
            }
            summary["cross_store_drift"][workspace_id] = findings

            for store_name, finding in findings.items():
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
                            workspace_id,
                            store_name,
                            store_name,
                            json.dumps(finding),
                        )
                    except Exception as exc:
                        logger.warning("cross_store_drift insert failed: %s", exc)
    finally:
        if qc is not None:
            await qc.close()

    return summary
