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
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime
from app.db import bind_workspace_scope

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
                    await bind_workspace_scope(
                        conn, workspace_id=str(ws_a), site="phase0.tenant_isolation_auditor",
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

    # Escalation on any violation — 2026-07-25 (Kestra retirement).
    #
    # Cross-tenant findings are critical, so the on-call channel has to
    # hear about them. This previously POSTed straight at Kestra's
    # execution API, gated on KESTRA_URL — a variable set in no
    # .env.example and no compose service env. That means this escalation
    # has been taking the `KESTRA_URL unset` branch and silently doing
    # nothing: the alarm for RLS tenancy violations never actually rang.
    #
    # Now enqueued onto the outbox `external_webhook` target, which is
    # HMAC-signed, retried, and dead-lettered with an audit row per
    # attempt. If no webhook URL is configured the row dead-letters
    # visibly instead of vanishing.
    #
    # Deliberately NOT idempotent-suppressed across runs: each nightly
    # audit that still finds violations should re-notify, because an
    # unresolved tenancy leak staying quiet is the failure mode this
    # exists to prevent. The key includes the run's violation count and
    # date so repeat findings re-fire while a single run can't double-send.
    if summary["violations"] > 0:
        audit_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            await rt.pg_pool.execute(
                """
                INSERT INTO outbox.pending_propagations
                    (workspace_id, source_schema, source_table, source_id,
                     target_store, target_collection, operation,
                     payload, idempotency_key)
                VALUES (NULL, 'audit', 'tenant_isolation', $1,
                        'external_webhook', 'security_critical', 'upsert',
                        $2::jsonb, $3)
                ON CONFLICT (target_store, idempotency_key)
                    WHERE status IN ('pending', 'in_flight')
                    DO NOTHING
                """,
                audit_day,
                json.dumps({
                    "severity": "critical",
                    "source": "tenant_isolation_auditor",
                    "violations": summary["violations"],
                    "summary": summary["violation_details"][:5],
                    "audit_day": audit_day,
                }),
                f"tenant_isolation:{audit_day}:{summary['violations']}",
            )
            summary["escalation_enqueued"] = True
        except Exception as exc:  # noqa: BLE001 — never fail the audit on notify
            summary["escalation_enqueued"] = False
            summary["escalation_error"] = f"{type(exc).__name__}:{exc}"
            logger.warning("tenant isolation escalation enqueue failed: %s", exc)

    return summary
