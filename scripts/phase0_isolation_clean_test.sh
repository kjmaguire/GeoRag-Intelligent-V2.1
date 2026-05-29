#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_isolation_clean_test.sh
#
# Phase 0 §Step 8 Test 6 — Tenant Isolation Auditor zero-violations.
#
# Invokes the Tenant Isolation Auditor agent against a synthetic clean
# workspace and asserts:
#   1. Agent exits with outcome='success' (not 'failure' or 'refusal').
#   2. Zero violations written to silver.store_reconciliation_findings.
#   3. The agent.invoke.success audit_ledger row exists with the
#      expected agent_name in payload.
#
# Exit 0 = clean pass. Exit 1 = violation detected OR agent crash.
#
# Replaces the equivalent check bundled inside phase0_step6_verify.sh
# so the v2.0 acceptance contract has a dedicated standalone script.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

WS_ID="${WS_ID:-00000000-acce-c1ea-aaaa-000000000006}"

cleanup() {
    $PG_PSQL_BIN -q -c "
        DELETE FROM audit.audit_ledger WHERE workspace_id = '${WS_ID}';
        DELETE FROM silver.store_reconciliation_findings WHERE workspace_id = '${WS_ID}';
        DELETE FROM silver.workspaces WHERE workspace_id = '${WS_ID}';
    " >/dev/null
}
trap cleanup EXIT
cleanup  # start clean

cat <<BANNER

============================================================
PHASE 0 STEP 8 TEST 6 — TENANT ISOLATION AUDITOR ZERO-VIOLATIONS
============================================================
Workspace: ${WS_ID}
============================================================
BANNER

# Seed a synthetic workspace row so RLS-aware probes have a target.
$PG_PSQL_BIN -q -c "
    INSERT INTO silver.workspaces (workspace_id, name, slug)
    VALUES ('${WS_ID}', 'phase0-isolation-clean-test',
            'phase0-isolation-clean-${WS_ID:0:8}')
    ON CONFLICT (workspace_id) DO NOTHING;
" >/dev/null

echo
echo "--- Invoking Tenant Isolation Auditor ---"
export WS_ID
export REDIS_PASSWORD="${REDIS_PASSWORD:-$(grep -E '^REDIS_PASSWORD=' "${HERE}/../.env" 2>/dev/null | cut -d= -f2- || echo)}"

outcome=$(fastapi_python_with_env WS_ID REDIS_PASSWORD -- -c "
import asyncio, asyncpg, os, sys, uuid
import redis.asyncio as aioredis
sys.path.insert(0, '/app')
from app.agents import register_runtime, AgentContext
from app.agents.phase0 import tenant_isolation_audit

DSN = ('postgres://'+os.environ.get('POSTGRES_USER','georag')
       + ':'+os.environ['POSTGRES_PASSWORD']
       + '@postgresql:5432/'+os.environ.get('POSTGRES_DB','georag'))

async def main() -> int:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2, statement_cache_size=0)
    redis = aioredis.from_url(
        'redis://:'+os.environ['REDIS_PASSWORD']+'@redis:6379/0',
        decode_responses=True,
    )
    register_runtime(pg_pool=pool, redis=redis)
    ws = uuid.UUID(os.environ['WS_ID'])
    r = await tenant_isolation_audit(
        ctx=AgentContext(workspace_id=ws),
        probes_per_table=3,
    )
    await pool.close()
    await redis.aclose()
    print(r.outcome)
    return 0

sys.exit(asyncio.run(main()))
" 2>&1 | tail -1 | tr -d ' \r')

echo "  outcome: ${outcome}"

# Assert 1: outcome must be 'success'
if [ "$outcome" != "success" ]; then
    echo "  [FAIL] Tenant Isolation Auditor outcome=${outcome} (expected success)"
    exit 1
fi
echo "  [PASS] outcome=success"

# Assert 2: zero violations written to findings table
n_violations=$($PG_PSQL_BIN -tAc "
    SELECT count(*) FROM silver.store_reconciliation_findings
    WHERE workspace_id = '${WS_ID}'
      AND severity = 'critical'
      AND discovered_by = 'Tenant Isolation Auditor';" | tr -d ' \r')

if [ "$n_violations" != "0" ]; then
    echo "  [FAIL] ${n_violations} tenant-isolation violation(s) detected (expected 0)"
    exit 1
fi
echo "  [PASS] zero violations in silver.store_reconciliation_findings"

# Assert 3: agent.invoke.success audit_ledger row exists
audit_row=$($PG_PSQL_BIN -tAc "
    SELECT count(*) FROM audit.audit_ledger
    WHERE workspace_id = '${WS_ID}'
      AND action_type = 'agent.invoke.success'
      AND payload->>'agent_name' = 'Tenant Isolation Auditor'
      AND created_at > now() - interval '5 minutes';" | tr -d ' \r')

if [ "$audit_row" -lt "1" ]; then
    echo "  [FAIL] no agent.invoke.success audit_ledger row for Tenant Isolation Auditor"
    exit 1
fi
echo "  [PASS] audit_ledger has agent.invoke.success row"

echo
echo "============================================================"
echo "PHASE 0 STEP 8 TEST 6 — PASS"
echo "============================================================"
exit 0
