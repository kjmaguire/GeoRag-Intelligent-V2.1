"""Phase 0 step 6 smoke — invokes each Phase 0 agent and verifies it

  1. completes (outcome=success — or 'refusal' for the LLM agents in the
     well-defined cases)
  2. emits an audit_ledger row with action_type='agent.invoke.<outcome>'
     containing the agent's name in the payload
  3. for the diagnostic agents, writes the expected output rows
     (findings, aggregates, support_packets, etc.) to silver/usage tables.

Runs inside georag-fastapi.

This script covers all 10 Python-implementable Phase 0 agents; the 11th
(GPU/VRAM Health) is Prometheus-rules only and is verified by the
phase0_step6_verify.sh wrapper, not here.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import asyncpg
import redis.asyncio as aioredis

sys.path.insert(0, "/app")
from app.agents import register_runtime, AgentContext  # noqa: E402
from app.agents.phase0 import (  # noqa: E402
    index_health_check,
    lineage_walk,
    llm_incident_diagnosis_run,
    model_cost_summary_run,
    model_upgrade_watch_run,
    storage_tiering_run,
    store_reconciliation_run,
    support_packet_assemble,
    tenant_isolation_audit,
    vllm_security_check_run,
)


WS_ID = uuid.UUID(os.environ["WS_ID"])
DB_DSN = (
    "postgres://"
    + os.environ.get("POSTGRES_USER", "georag")
    + ":" + os.environ["POSTGRES_PASSWORD"]
    + "@postgresql:5432/"
    + os.environ.get("POSTGRES_DB", "georag")
)
REDIS_URL = "redis://:" + os.environ["REDIS_PASSWORD"] + "@redis:6379/0"


def _ok(name: str, value: object) -> str:
    return f"  [agent] {name} → {value}"


async def main() -> int:
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4, statement_cache_size=0)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    register_runtime(pg_pool=pool, redis=redis)
    fail: list[str] = []

    # ---- 1. Tenant Isolation Auditor --------------------------------------
    r1 = await tenant_isolation_audit(
        ctx=AgentContext(workspace_id=WS_ID, actor_kind="system"),
        probes_per_table=2,
    )
    print(_ok(
        "Tenant Isolation Auditor",
        f"outcome={r1.outcome} violations={r1.value.get('violations')} "
        f"probes_run={r1.value.get('probes_run')}",
    ))
    if r1.outcome != "success":
        fail.append(f"Tenant Isolation outcome={r1.outcome} error={r1.error}")

    # ---- 2. Lineage Reporter ----------------------------------------------
    r2 = await lineage_walk(
        ctx=AgentContext(workspace_id=WS_ID, actor_kind="system"),
        target_type="workspace",
        target_id=str(WS_ID),
        limit=50,
    )
    chain_len = r2.value.get("chain_length", 0)
    print(_ok("Lineage Reporter Agent", f"outcome={r2.outcome} chain_length={chain_len}"))
    if r2.outcome != "success":
        fail.append(f"Lineage outcome={r2.outcome} error={r2.error}")
    if chain_len < 1:
        fail.append(f"Lineage chain_length={chain_len} (expected >= 1)")

    # ---- 3. Index Health --------------------------------------------------
    r3 = await index_health_check(
        ctx=AgentContext(workspace_id=WS_ID, actor_kind="system"),
        slow_query_ms_threshold=5000.0,
        bloat_dead_tup_ratio_threshold=0.50,
    )
    print(_ok(
        "Index Health Agent",
        f"outcome={r3.outcome} slow={r3.value.get('slow_queries_flagged')} "
        f"bloat={r3.value.get('bloat_findings')}",
    ))
    if r3.outcome != "success":
        fail.append(f"Index Health outcome={r3.outcome} error={r3.error}")

    # ---- 4. Store Reconciliation ------------------------------------------
    await pool.execute(
        """
        INSERT INTO outbox.pending_propagations
          (workspace_id, source_schema, source_table, source_id,
           target_store, operation, payload, idempotency_key, status,
           dead_lettered_at, enqueued_at)
        VALUES
          ($1, 'silver', 'document_passages', 'src-dead-1', 'qdrant', 'upsert',
           '{}'::jsonb, 'phase0-smoke-dead-' || gen_random_uuid()::text,
           'dead_lettered', now() - interval '1 hour', now() - interval '2 hours'),
          ($1, 'silver', 'document_passages', 'src-stuck-1', 'qdrant', 'upsert',
           '{}'::jsonb, 'phase0-smoke-stuck-' || gen_random_uuid()::text,
           'in_flight', NULL, now() - interval '3 hours'),
          ($1, 'silver', 'document_passages', 'src-missing-1', 'qdrant', 'upsert',
           '{}'::jsonb, 'phase0-smoke-missing-' || gen_random_uuid()::text,
           'pending', NULL, now() - interval '2 hours')
        """,
        str(WS_ID),
    )
    await pool.execute(
        """
        UPDATE outbox.pending_propagations
           SET last_attempted_at = now() - interval '3 hours'
         WHERE workspace_id = $1 AND source_id = 'src-stuck-1' AND status = 'in_flight'
        """,
        str(WS_ID),
    )
    r4 = await store_reconciliation_run(
        ctx=AgentContext(workspace_id=WS_ID, actor_kind="system"),
        stuck_threshold_minutes=60,
        missing_threshold_minutes=60,
    )
    print(_ok(
        "Store Reconciliation Agent",
        f"outcome={r4.outcome} dead={r4.value.get('dead_lettered')} "
        f"stuck={r4.value.get('stuck')} missing={r4.value.get('missing_in_b')}",
    ))
    if r4.outcome != "success":
        fail.append(f"Store Reconciliation outcome={r4.outcome} error={r4.error}")
    if not (r4.value.get("dead_lettered", 0) >= 1
            and r4.value.get("stuck", 0) >= 1
            and r4.value.get("missing_in_b", 0) >= 1):
        fail.append(
            "Store Reconciliation didn't flag all 3 seeded findings: "
            f"dead={r4.value.get('dead_lettered')} stuck={r4.value.get('stuck')} "
            f"missing={r4.value.get('missing_in_b')}"
        )

    # ---- 5. Storage Tiering -----------------------------------------------
    # R2 agent — bypass the document-id idempotency requirement for a
    # policy-level run. dry_run_s3=True so we exercise the rule loop +
    # bucket-list path without depending on SeaweedFS having data
    # already (which the dev env doesn't, in Phase 0).
    r5 = await storage_tiering_run(
        ctx=AgentContext(
            workspace_id=WS_ID,
            actor_kind="system",
            bypass_idempotency=True,
        ),
        max_objects_per_rule=10,
        dry_run_s3=True,
    )
    print(_ok(
        "Storage Tiering Agent",
        f"outcome={r5.outcome} rules_evaluated={r5.value.get('rules_evaluated')} "
        f"errors={r5.value.get('errors')}",
    ))
    if r5.outcome != "success":
        fail.append(f"Storage Tiering outcome={r5.outcome} error={r5.error}")
    if r5.value.get("rules_evaluated", 0) < 1:
        fail.append("Storage Tiering: rules_evaluated == 0 (expected ≥1 platform default)")

    # ---- 6. Model Upgrade Watch -------------------------------------------
    r6 = await model_upgrade_watch_run(
        ctx=AgentContext(workspace_id=WS_ID, actor_kind="system"),
        timeout_s=6.0,
    )
    print(_ok(
        "Model Upgrade Watch Agent",
        f"outcome={r6.outcome} vllm_checked={r6.value.get('vllm', {}).get('checked')} "
        f"model_checked={r6.value.get('model', {}).get('checked')}",
    ))
    if r6.outcome != "success":
        fail.append(f"Model Upgrade Watch outcome={r6.outcome} error={r6.error}")

    # ---- 7. vLLM Security Check -------------------------------------------
    r7 = await vllm_security_check_run(
        ctx=AgentContext(workspace_id=WS_ID, actor_kind="system"),
        timeout_s=6.0,
    )
    print(_ok(
        "vLLM Security Check Agent",
        f"outcome={r7.outcome} advisories_seen={r7.value.get('advisories_seen')} "
        f"matches={len(r7.value.get('matches') or [])}",
    ))
    if r7.outcome != "success":
        fail.append(f"vLLM Security Check outcome={r7.outcome} error={r7.error}")

    # ---- 8. Model Cost Summary --------------------------------------------
    # Seed a synthetic usage_events row so the rollup has work.
    yesterday = date.today() - timedelta(days=1)
    yesterday_ts = datetime(yesterday.year, yesterday.month, yesterday.day, 12, 0, tzinfo=timezone.utc)
    await pool.execute(
        """
        INSERT INTO usage.usage_events
            (workspace_id, agent_name, model_profile, model_id,
             tokens_prompt, tokens_completion, projected_cost_usd,
             outcome, created_at)
        VALUES ($1, 'phase0-smoke-agent', 'chat_deep', 'phase0-smoke',
                100, 250, 0.000123, 'success', $2)
        """,
        str(WS_ID),
        yesterday_ts,
    )
    r8 = await model_cost_summary_run(
        ctx=AgentContext(workspace_id=WS_ID, actor_kind="system"),
        rollup_date=yesterday,
    )
    print(_ok(
        "Model Cost Summary Agent",
        f"outcome={r8.outcome} rows_aggregated={r8.value.get('rows_aggregated')} "
        f"buckets_upserted={r8.value.get('buckets_upserted')}",
    ))
    if r8.outcome != "success":
        fail.append(f"Model Cost Summary outcome={r8.outcome} error={r8.error}")
    if r8.value.get("buckets_upserted", 0) < 1:
        fail.append("Model Cost Summary: buckets_upserted == 0 (expected ≥1 from seeded usage row)")

    # ---- 9. LLM Incident Diagnosis ----------------------------------------
    # Empty alert + zero context → must REFUSE (well-defined refusal path).
    r9 = await llm_incident_diagnosis_run(
        ctx=AgentContext(workspace_id=WS_ID, actor_kind="system"),
        alert_label="",
        window_minutes=1,
    )
    print(_ok(
        "LLM Incident Diagnosis Agent",
        f"outcome={r9.outcome} (empty-context path → expected refusal) error={r9.error!r}",
    ))
    if r9.outcome != "refusal":
        fail.append(
            f"LLM Incident Diagnosis: expected outcome='refusal' on empty input, "
            f"got outcome={r9.outcome} error={r9.error}"
        )

    # ---- 10. Support Packet Agent -----------------------------------------
    incident_id = f"phase0-smoke-{uuid.uuid4()}"
    r10 = await support_packet_assemble(
        ctx=AgentContext(
            workspace_id=WS_ID,
            document_id=incident_id,
            actor_kind="system",
        ),
        incident_id=incident_id,
        trace_id=None,
    )
    print(_ok(
        "Support Packet Agent",
        f"outcome={r10.outcome} bundle_bytes={(r10.value or {}).get('bundle_bytes')} "
        f"upload_ok={(r10.value or {}).get('upload_ok')}",
    ))
    if r10.outcome != "success":
        fail.append(f"Support Packet outcome={r10.outcome} error={r10.error}")
    # The silver.support_packets row should exist (regardless of upload success).
    # silver.support_packets has STRICT RLS (no escape-hatch), so we must set
    # the workspace_id GUC inside the verifying transaction or RLS will hide
    # the row we just wrote (R-P0-10 fallout — see support_packet.py for the
    # canonical write pattern).
    async with pool.acquire() as _conn:
        async with _conn.transaction():
            await _conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)", str(WS_ID)
            )
            n_packets = await _conn.fetchval(
                "SELECT count(*) FROM silver.support_packets "
                "WHERE workspace_id = $1 AND incident_id = $2",
                WS_ID,
                incident_id,
            )
    if n_packets != 1:
        fail.append(f"Support Packet: expected 1 silver.support_packets row, got {n_packets}")

    # ---- Audit-trail check -------------------------------------------------
    expected_agents = {
        "Tenant Isolation Auditor",
        "Lineage Reporter Agent",
        "Index Health Agent",
        "Store Reconciliation Agent",
        "Storage Tiering Agent",
        "Model Upgrade Watch Agent",
        "vLLM Security Check Agent",
        "Model Cost Summary Agent",
        "LLM Incident Diagnosis Agent",
        "Support Packet Agent",
    }
    rows = await pool.fetch(
        """
        SELECT payload->>'agent_name' AS agent_name,
               action_type
        FROM audit.audit_ledger
        WHERE action_type LIKE 'agent.invoke.%'
          AND created_at >= now() - interval '5 minutes'
          AND payload->>'agent_name' = ANY($1)
        """,
        list(expected_agents),
    )
    found = {r["agent_name"] for r in rows}
    missing = expected_agents - found
    print(f"  [audit ] agent.invoke.* rows for {len(found)}/{len(expected_agents)} agents")
    if missing:
        fail.append(f"missing audit_ledger entries for: {sorted(missing)}")

    # Storage Tiering should also emit at least ONE 'agent.invoke.*' row;
    # the 'storage.tier_transition' row only fires if a real object moves
    # — Phase 0 dev env has no objects, so we don't require that.

    # Support Packet — the agent should have written its own
    # 'support_packet.assembled' audit_ledger row.
    n_assembled = await pool.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
        WHERE action_type = 'support_packet.assembled'
          AND workspace_id = $1
          AND created_at >= now() - interval '5 minutes'
        """,
        WS_ID,
    )
    if n_assembled < 1:
        fail.append(
            "Support Packet: expected at least 1 'support_packet.assembled' "
            f"audit_ledger row, got {n_assembled}"
        )

    await pool.close()
    await redis.close()

    print()
    if fail:
        print("FAIL:")
        for f in fail:
            print("  -", f)
        return 1
    print("ALL 10 AGENTS PASSED + AUDIT TRAIL CONFIRMED")
    return 0


sys.exit(asyncio.run(main()))
