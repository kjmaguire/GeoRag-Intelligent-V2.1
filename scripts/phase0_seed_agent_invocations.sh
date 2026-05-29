#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_seed_agent_invocations.sh
#
# Kickoff #12 — schedule each Phase 0 agent once against a synthetic
# workspace so the audit_ledger contains at least one `agent.invoke.success`
# row per agent. Provides the "live invocation evidence" the acceptance
# harness queries via the §Step 6 overall DoD.
#
# Idempotent: re-running adds more rows; cleanup is left to the caller.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

WS_ID="${WS_ID:-00000000-acce-9999-aaaa-000000000012}"

# Seed workspace row (FK target for audit_ledger).
$PG_PSQL_BIN -q -c "
    INSERT INTO silver.workspaces (workspace_id, name, slug)
    VALUES ('${WS_ID}', 'phase0-agent-seed', 'phase0-agent-seed-${WS_ID:0:8}')
    ON CONFLICT (workspace_id) DO NOTHING;
" >/dev/null

echo "==> Seeding one invocation per Phase 0 agent against ${WS_ID:0:8}…"

export WS_ID
export REDIS_PASSWORD="${REDIS_PASSWORD:-$(grep -E '^REDIS_PASSWORD=' "${HERE}/../.env" 2>/dev/null | cut -d= -f2- || echo)}"
fastapi_python_with_env WS_ID REDIS_PASSWORD -- -c "
import asyncio, asyncpg, os, sys, uuid
import redis.asyncio as aioredis
sys.path.insert(0, '/app')
from app.agents import register_runtime, AgentContext
from app.agents.phase0 import (
    tenant_isolation_audit,
    lineage_walk,
    storage_tiering_run,
    index_health_check,
    store_reconciliation_run,
    model_upgrade_watch_run,
    vllm_security_check_run,
    model_cost_summary_run,
    llm_incident_diagnosis_run,
    support_packet_assemble,
)

DSN = ('postgres://'+os.environ.get('POSTGRES_USER','georag')
       + ':'+os.environ['POSTGRES_PASSWORD']
       + '@postgresql:5432/'+os.environ.get('POSTGRES_DB','georag'))

async def main() -> int:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, statement_cache_size=0)
    redis = aioredis.from_url(
        'redis://:'+os.environ['REDIS_PASSWORD']+'@redis:6379/0',
        decode_responses=True,
    )
    register_runtime(pg_pool=pool, redis=redis)
    ws = uuid.UUID(os.environ['WS_ID'])
    ctx = AgentContext(workspace_id=ws)

    outcomes: list[tuple[str, str]] = []
    invocations = [
        ('tenant_isolation_audit', tenant_isolation_audit(ctx=ctx, probes_per_table=1)),
        ('lineage_walk',           lineage_walk(ctx=ctx, target_type='workspace', target_id=str(ws), limit=5)),
        ('storage_tiering_run',    storage_tiering_run(ctx=ctx, dry_run=True)),
        ('index_health_check',     index_health_check(ctx=ctx, slow_query_ms_threshold=99999.0)),
        ('store_reconciliation_run', store_reconciliation_run(ctx=ctx)),
        ('model_upgrade_watch_run',  model_upgrade_watch_run(ctx=ctx)),
        ('vllm_security_check_run',  vllm_security_check_run(ctx=ctx)),
        ('model_cost_summary_run',   model_cost_summary_run(ctx=ctx)),
        ('llm_incident_diagnosis_run', llm_incident_diagnosis_run(ctx=ctx, incident_window_minutes=5)),
        ('support_packet_assemble',    support_packet_assemble(ctx=ctx, incident_id='phase0-seed')),
    ]
    for name, coro in invocations:
        try:
            r = await coro
            outcomes.append((name, getattr(r, 'outcome', 'unknown')))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((name, f'exception:{type(exc).__name__}'))

    await pool.close()
    await redis.close()

    for name, outcome in outcomes:
        print(f'  {name}: {outcome}')
    return 0

sys.exit(asyncio.run(main()))
"
