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

    # B1 (2026-07-28): Neo4j was removed from the stack. The three Cypher
    # invariants this agent used to run (node workspace_id coverage,
    # cross-workspace edges, orphan cross-store consistency) are gone.
    # ``summary`` keeps the same fail-open shape the try/except used to
    # leave behind when the driver was unreachable — ``neo4j_reachable``
    # stays False (its dict default above) and ``skipped_reason`` explains
    # why — so the findings-persist and run-summary sections below still
    # execute exactly as they did on the live "neo4j unreachable" path.
    summary["skipped_reason"] = "neo4j was removed from the stack (B1, 2026-07-28)"

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
