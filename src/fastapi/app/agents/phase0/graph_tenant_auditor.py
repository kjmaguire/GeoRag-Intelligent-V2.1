"""Graph Tenant Auditor (Phase 0 — Z-roadmap Z.9).

Neo4j-side companion to ``tenant_isolation_auditor`` (which audits
Postgres RLS). Runs three Cypher-driven invariants documented in
Appendix H §6 "Workspace isolation — the fence":

  1. **node_workspace_id_coverage**
     Every node carries a non-null ``workspace_id`` property, except
     labels explicitly listed in ``_NODE_WORKSPACE_ID_EXEMPT`` (open-data
     / catalogue nodes shared across tenants). Any violation is a Phase 0
     R0 critical finding.

  2. **edge_cross_workspace_check**
     Zero relationships exist where the startNode and endNode carry
     different ``workspace_id`` values. A cross-workspace edge is a
     direct tenant-fence breach.

  3. **orphan_cross_store_consistency**
     Every silver-side entity that the graph claims to mirror is actually
     present. Two probes:
        - silver.projects.project_id rows for the workspace that have no
          ``(:Project {project_id})`` node → "missing in graph"
        - graph ``(:Project {workspace_id})`` nodes whose ``project_id``
          is not present in silver.projects → "orphan in graph"

All findings are persisted to ``silver.store_reconciliation_findings``
with discovered_by='Graph Tenant Auditor' (the same per-row table the
PG auditor writes to). The agent return summary also writes one row to
``silver.tenant_isolation_audit`` so the ops dashboard can chart
isolation health over time.

Neo4j driver is lazily imported and a missing driver is a no-op — same
defensive pattern as ``store_reconciliation.py``. The agent is safe to
schedule even in environments where Neo4j is offline.

Cypher style follows CLAUDE.md hard rule #9 + the project conventions:
parameterised queries only, lowercase node variables (``n``, ``e``,
``p``), and Community-Edition-compatible syntax (no Enterprise
features).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime

logger = logging.getLogger(__name__)


# Labels exempt from the per-node workspace_id requirement. The
# Appendix H schema treats every node as tenant-scoped *except* the
# reserved ``:Internal`` namespace described in the spec (used for
# platform-wide ontology / catalogue nodes shared across tenants). Add
# new exemptions here with an inline justification — every entry is a
# tenant-fence carve-out and deserves the audit trail.
_NODE_WORKSPACE_ID_EXEMPT: frozenset[str] = frozenset(
    {
        # Reserved platform-wide namespace. Appendix H §6 calls this out
        # explicitly; nothing in the kg_sync writers creates :Internal
        # nodes today, but the auditor honours the reservation so a
        # future ontology-merge job doesn't accidentally trip the gate.
        "Internal",
    }
)


def _neo4j_uri() -> str:
    return os.environ.get("NEO4J_URI", "bolt://neo4j:7687")


def _neo4j_auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_USERNAME", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", ""),
    )


def _persist_run_summary_sql_args(
    summary: dict[str, Any],
    workspace_id: UUID | None,
) -> tuple[str, list[Any]]:
    """Build the INSERT for the run-level row in
    ``silver.tenant_isolation_audit``. Returns (sql, args) so the caller
    decides whether to write — keeps the function pure for unit tests.
    """
    return (
        """
        INSERT INTO silver.tenant_isolation_audit
            (workspace_id, auditor, pg_violations, graph_violations,
             tables_probed, edges_probed, nodes_probed,
             violation_details, finished_at)
        VALUES ($1, 'neo4j_graph', 0, $2, $3, $4, $5, $6::jsonb, now())
        """,
        [
            workspace_id,
            int(summary["graph_violations"]),
            int(summary["labels_probed"]),
            int(summary["edges_probed"]),
            int(summary["nodes_probed"]),
            json.dumps(
                {
                    "missing_workspace_id": summary["missing_workspace_id_details"],
                    "cross_workspace_edges": summary["cross_workspace_edge_details"],
                    "orphan_nodes": summary["orphan_node_details"],
                }
            ),
        ],
    )


@georag_agent(
    name="Graph Tenant Auditor",
    risk_tier="R0",
    version="0.1.0",
)
async def graph_tenant_audit(
    ctx: AgentContext,
    *,
    sample_limit_per_check: int = 25,
) -> dict[str, Any]:
    """Run the three graph-tenancy invariants and persist findings.

    ``sample_limit_per_check`` bounds the number of offending rows
    reported per check; the auditor still counts ALL violations via
    ``count(*)``, only the per-row details list is capped to keep the
    silver finding rows from exploding under a wide breach.
    """
    rt = get_runtime()
    summary: dict[str, Any] = {
        "labels_probed": 0,
        "nodes_probed": 0,
        "edges_probed": 0,
        "graph_violations": 0,
        "missing_workspace_id": 0,
        "missing_workspace_id_details": [],
        "cross_workspace_edges": 0,
        "cross_workspace_edge_details": [],
        "orphan_nodes": 0,
        "orphan_node_details": [],
        "neo4j_reachable": False,
    }

    try:
        from neo4j import AsyncGraphDatabase  # noqa: PLC0415
    except ImportError:
        logger.info("graph_tenant_audit: neo4j driver not installed — skip")
        summary["skipped_reason"] = "neo4j driver not installed"
        return summary

    driver = AsyncGraphDatabase.driver(_neo4j_uri(), auth=_neo4j_auth())
    try:
        async with driver.session() as session:
            summary["neo4j_reachable"] = True

            # ---- Check 1: every node carries workspace_id --------------
            # Exempt labels are skipped via a list parameter and the
            # ``none(...)`` predicate so the query is one round-trip
            # regardless of how many exemptions exist. Lowercase node
            # variable per CLAUDE.md Cypher style.
            exempt_list = list(_NODE_WORKSPACE_ID_EXEMPT)
            missing_ws_q = (
                "MATCH (n) "
                "WHERE n.workspace_id IS NULL "
                "  AND none(lbl IN labels(n) WHERE lbl IN $exempt) "
                "RETURN labels(n) AS labels, count(n) AS missing_count, "
                "       collect(elementId(n))[..$limit] AS sample_ids"
            )
            res = await session.run(
                missing_ws_q,
                exempt=exempt_list,
                limit=sample_limit_per_check,
            )
            async for record in res:
                missing = int(record["missing_count"] or 0)
                if missing == 0:
                    continue
                summary["missing_workspace_id"] += missing
                summary["graph_violations"] += missing
                summary["missing_workspace_id_details"].append(
                    {
                        "labels": list(record["labels"] or []),
                        "missing_count": missing,
                        "sample_element_ids": list(record["sample_ids"] or []),
                    }
                )
                summary["labels_probed"] += 1

            # Total node count for the run summary.
            res = await session.run("MATCH (n) RETURN count(n) AS c")
            rec = await res.single()
            summary["nodes_probed"] = int(rec["c"] if rec else 0)

            # ---- Check 2: cross-workspace edges ------------------------
            # The fence violation: a relationship whose endpoints carry
            # different non-null workspace_id values. NULL on either end
            # is already covered by Check 1; we explicitly exclude that
            # case here so violations don't double-count.
            cross_edge_q = (
                "MATCH (a)-[e]->(b) "
                "WHERE a.workspace_id IS NOT NULL "
                "  AND b.workspace_id IS NOT NULL "
                "  AND a.workspace_id <> b.workspace_id "
                "RETURN type(e) AS rel_type, "
                "       a.workspace_id AS ws_a, "
                "       b.workspace_id AS ws_b, "
                "       count(e) AS violations, "
                "       collect(elementId(e))[..$limit] AS sample_ids"
            )
            res = await session.run(cross_edge_q, limit=sample_limit_per_check)
            async for record in res:
                violations = int(record["violations"] or 0)
                if violations == 0:
                    continue
                summary["cross_workspace_edges"] += violations
                summary["graph_violations"] += violations
                summary["cross_workspace_edge_details"].append(
                    {
                        "rel_type": record["rel_type"],
                        "ws_a": str(record["ws_a"]),
                        "ws_b": str(record["ws_b"]),
                        "violations": violations,
                        "sample_element_ids": list(record["sample_ids"] or []),
                    }
                )

            # Total edge count for the run summary.
            res = await session.run("MATCH ()-[e]->() RETURN count(e) AS c")
            rec = await res.single()
            summary["edges_probed"] = int(rec["c"] if rec else 0)

            # ---- Check 3: orphan / missing project cross-store --------
            # Only runs when the agent invocation is workspace-scoped —
            # the silver.projects probe is workspace-filtered and a
            # system-wide sweep would have to fan out to every workspace
            # which we defer to Phase 1.
            if ctx.workspace_id is not None:
                # 3a — silver projects MISSING from graph
                pg_rows = await rt.pg_pool.fetch(
                    """
                    SELECT project_id::text AS project_id, project_name
                      FROM silver.projects
                     WHERE workspace_id = $1
                    """,
                    ctx.workspace_id,
                )
                if pg_rows:
                    pg_ids = [r["project_id"] for r in pg_rows]
                    res = await session.run(
                        "UNWIND $ids AS pid "
                        "OPTIONAL MATCH (p:Project {project_id: pid, "
                        "                           workspace_id: $ws}) "
                        "WITH pid, p WHERE p IS NULL "
                        "RETURN collect(pid) AS missing",
                        ids=pg_ids,
                        ws=str(ctx.workspace_id),
                    )
                    rec = await res.single()
                    missing = list(rec["missing"] if rec else [])
                    if missing:
                        summary["orphan_nodes"] += len(missing)
                        summary["graph_violations"] += len(missing)
                        summary["orphan_node_details"].append(
                            {
                                "kind": "missing_in_graph",
                                "label": "Project",
                                "count": len(missing),
                                "sample_project_ids": missing[:sample_limit_per_check],
                            }
                        )

                # 3b — graph project nodes ORPHANED from silver
                res = await session.run(
                    "MATCH (p:Project {workspace_id: $ws}) "
                    "RETURN collect(p.project_id) AS ids",
                    ws=str(ctx.workspace_id),
                )
                rec = await res.single()
                graph_ids = [
                    pid for pid in (rec["ids"] if rec else []) if pid
                ]
                if graph_ids:
                    silver_rows = await rt.pg_pool.fetch(
                        """
                        SELECT project_id::text AS project_id
                          FROM silver.projects
                         WHERE workspace_id = $1
                           AND project_id::text = ANY($2::text[])
                        """,
                        ctx.workspace_id,
                        graph_ids,
                    )
                    silver_set = {r["project_id"] for r in silver_rows}
                    orphans = [pid for pid in graph_ids if pid not in silver_set]
                    if orphans:
                        summary["orphan_nodes"] += len(orphans)
                        summary["graph_violations"] += len(orphans)
                        summary["orphan_node_details"].append(
                            {
                                "kind": "orphan_in_graph",
                                "label": "Project",
                                "count": len(orphans),
                                "sample_project_ids": orphans[:sample_limit_per_check],
                            }
                        )
            else:
                summary["orphan_check_skipped_reason"] = (
                    "workspace_id not set on context — orphan probe runs per-workspace only"
                )
    finally:
        await driver.close()

    # ---- Persist per-row findings to store_reconciliation_findings ----
    # Each violation kind becomes one finding row with severity='critical'
    # — same shape the PG auditor writes, so the ops dashboard can union
    # both stores without schema-special-casing.
    finding_writes: list[tuple[str, str, dict[str, Any]]] = []
    for d in summary["missing_workspace_id_details"]:
        finding_writes.append(("missing_in_b", "neo4j", d))
    for d in summary["cross_workspace_edge_details"]:
        finding_writes.append(("orphan_in_b", "neo4j", d))
    for d in summary["orphan_node_details"]:
        drift = "missing_in_b" if d.get("kind") == "missing_in_graph" else "orphan_in_b"
        finding_writes.append((drift, "neo4j", d))

    if finding_writes and ctx.workspace_id is not None:
        for drift_type, target_store, details in finding_writes:
            try:
                await rt.pg_pool.execute(
                    """
                    INSERT INTO silver.store_reconciliation_findings
                        (workspace_id, drift_type, severity, source_store,
                         target_store, source_id, details, discovered_by)
                    VALUES ($1, $2, 'critical', 'neo4j', $3, $4, $5::jsonb,
                            'Graph Tenant Auditor')
                    """,
                    ctx.workspace_id,
                    drift_type,
                    target_store,
                    details.get("rel_type") or details.get("label") or "",
                    json.dumps(details),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "graph_tenant_audit: finding insert failed: %s", exc
                )

    # ---- Persist the run-level row to silver.tenant_isolation_audit ---
    try:
        sql, args = _persist_run_summary_sql_args(summary, ctx.workspace_id)
        await rt.pg_pool.execute(sql, *args)
    except Exception as exc:  # noqa: BLE001
        # The audit-log table may not exist yet in environments where the
        # 2026_05_30 migration hasn't run — log + continue rather than
        # blocking the agent.
        logger.warning(
            "graph_tenant_audit: tenant_isolation_audit persist failed: %s", exc
        )
        summary["audit_log_persist_error"] = str(exc)

    return summary
