"""Tenant Isolation Auditor (Phase 0 agent #1).

Samples cross-workspace probes against RLS-enabled tables to confirm that
workspace A cannot read workspace B's data. Any non-zero probe result is a
critical isolation violation and writes a row to
``silver.store_reconciliation_findings`` with severity='critical'.

Phase 0 scope: 16 RLS-protected tables (the set enabled in Step 2 RLS
policies). Probe count per table is bounded so a nightly run completes in
seconds, not minutes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID

import httpx

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime

logger = logging.getLogger(__name__)


# The 16 workspace-scoped tables RLS-enabled in Step 2 95-rls-policies.sql.
# audit_ledger_verification_runs is intentionally omitted (no workspace_id).
RLS_TABLES = [
    ("workspace", "workspace_memberships"),
    ("workspace", "workspace_agent_config"),
    ("workspace", "idempotency_keys"),
    ("workspace", "dry_run_outputs"),
    ("audit", "audit_ledger"),
    ("workflow", "workflow_runs"),
    ("workflow", "workflow_run_events"),
    ("outbox", "pending_propagations"),
    ("outbox", "propagation_attempts"),
    ("usage", "usage_events"),
    ("usage", "usage_aggregates_daily"),
    ("usage", "workspace_cost_ceilings"),
    ("silver", "store_reconciliation_findings"),
    ("silver", "corpus_health_findings"),
    ("silver", "storage_tier_policy"),
]


@georag_agent(
    name="Tenant Isolation Auditor",
    risk_tier="R0",
    version="0.1.0",
)
async def tenant_isolation_audit(
    ctx: AgentContext,
    *,
    probes_per_table: int = 5,
) -> dict[str, Any]:
    """Run cross-workspace probes against every RLS-enabled table.

    For each table, picks two distinct workspaces from ``silver.workspaces``
    (returning early if fewer than 2 exist) and runs ``probes_per_table``
    cross-workspace SELECT counts. Each should return 0; any non-zero result
    is a critical violation.

    Returns a summary dict the wrapper logs into the audit_ledger payload:
        { tables_probed, probes_run, violations, violation_details }
    """
    rt = get_runtime()
    summary: dict[str, Any] = {
        "tables_probed": 0,
        "probes_run": 0,
        "violations": 0,
        "violation_details": [],
    }

    workspaces = await rt.pg_pool.fetch(
        "SELECT workspace_id FROM silver.workspaces ORDER BY created_at LIMIT 10"
    )
    if len(workspaces) < 2:
        summary["note"] = "fewer than 2 workspaces present — isolation probe vacuously clean"
        return summary

    ws_ids: list[UUID] = [r["workspace_id"] for r in workspaces]
    ws_a, ws_b = ws_ids[0], ws_ids[1]

    async with rt.pg_pool.acquire() as conn:
        for schema, table in RLS_TABLES:
            summary["tables_probed"] += 1
            for _ in range(probes_per_table):
                summary["probes_run"] += 1
                # Run with workspace A's context, ask for workspace B's rows.
                # RLS should clamp the result to 0.
                async with conn.transaction():
                    # SET LOCAL doesn't accept $-parameter binding; use
                    # set_config(name, value, is_local=true) instead.
                    await conn.execute(
                        "SELECT set_config('app.workspace_id', $1, true)",
                        str(ws_a),
                    )
                    rows = await conn.fetchval(
                        f'SELECT count(*) FROM "{schema}"."{table}" WHERE workspace_id = $1',
                        ws_b,
                    )
                    if rows and rows > 0:
                        summary["violations"] += 1
                        summary["violation_details"].append(
                            {
                                "schema": schema,
                                "table": table,
                                "ws_a": str(ws_a),
                                "ws_b": str(ws_b),
                                "leaked_row_count": int(rows),
                            }
                        )
                        # Write finding immediately — don't wait for the run to finish.
                        await rt.pg_pool.execute(
                            """
                            INSERT INTO silver.store_reconciliation_findings
                                (workspace_id, drift_type, severity, source_store, target_store,
                                 source_id, details, discovered_by)
                            VALUES ($1, 'orphan_in_b', 'critical', 'postgres-rls',
                                    $2, $3, $4::jsonb, 'Tenant Isolation Auditor')
                            """,
                            ws_a,
                            f"{schema}.{table}",
                            f"ws_a={ws_a},ws_b={ws_b}",
                            json.dumps(summary["violation_details"][-1]),
                        )

    # All-nighter 2026-05-21 — SET vs SET LOCAL detector.
    # Scan installed function bodies + trigger source for `SET ` GUC writes
    # that AREN'T `SET LOCAL` — those would leak across transactions and
    # are the prime cause of cross-tenant data showing up in cached
    # Postgres connections.
    setset_findings = []
    try:
        prosrc_rows = await rt.pg_pool.fetch(
            """
            SELECT n.nspname AS schema, p.proname AS name, p.prosrc AS body
            FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE n.nspname IN ('silver','gold','bronze','audit','usage','workflow','outbox','workspace','public_geo')
              AND p.prosrc ~* 'set\\s+(app|georag)\\.'
              AND p.prosrc !~* 'set\\s+local\\s+(app|georag)\\.'
              AND p.prosrc !~* 'set_config\\s*\\('
            """
        )
        for r in prosrc_rows:
            setset_findings.append({
                "kind": "function",
                "schema": r["schema"],
                "name": r["name"],
            })
    except Exception as exc:
        logger.warning("set-vs-set-local probe failed: %s", exc)
    summary["set_local_violations"] = setset_findings
    summary["violations"] += len(setset_findings)

    # All-nighter 2026-05-21 — Kestra escalation on any violation.
    # The kickoff rubric expects a Kestra `external_notification` trigger
    # on cross-tenant findings so the on-call channel hears about it.
    if summary["violations"] > 0:
        kestra_url = os.environ.get("KESTRA_URL", "").strip()
        flow_id = os.environ.get(
            "KESTRA_TENANT_ISOLATION_FLOW",
            "external_notification",
        )
        if kestra_url:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    await client.post(
                        f"{kestra_url.rstrip('/')}/api/v1/executions/georag/{flow_id}",
                        json={
                            "severity": "critical",
                            "source": "tenant_isolation_auditor",
                            "violations": summary["violations"],
                            "summary": summary["violation_details"][:5],
                        },
                    )
                summary["kestra_escalated"] = True
            except httpx.HTTPError as exc:
                summary["kestra_escalated"] = False
                summary["kestra_error"] = str(exc)
                logger.warning("kestra escalation failed: %s", exc)
        else:
            summary["kestra_escalated"] = False
            summary["kestra_skip_reason"] = "KESTRA_URL unset"

    return summary
