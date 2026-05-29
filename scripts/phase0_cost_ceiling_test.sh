#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_cost_ceiling_test.sh
#
# Phase 0 §Step 8 Test 10 — cost-ceiling soft-warn fire-drill.
#
# Steps:
#   1. Seed a synthetic workspace with a $10 monthly ceiling (soft-warn at 80%).
#   2. INSERT synthetic usage_events totalling $8.50 (85% of ceiling).
#   3. Invoke the Model Cost Summary Agent.
#   4. Assert: cost_ceiling.soft_warn audit_ledger row fires.
#   5. Verify: last_warn_pct on workspace_cost_ceilings = 85, last_warn_sent_at set.
#   6. Cleanup.
#
# Exit 0 = soft-warn fired correctly. Exit 1 = no warn or wrong threshold.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

WS_ID="${WS_ID:-00000000-acce-c1ea-bbbb-000000000010}"
CEILING_USD="10.00"
SYNTHETIC_SPEND_USD="8.50"  # 85% of $10 — must trip soft-warn at 80%

cleanup() {
    $PG_PSQL_BIN -q -c "
        DELETE FROM audit.audit_ledger WHERE workspace_id = '${WS_ID}';
        DELETE FROM usage.usage_events WHERE workspace_id = '${WS_ID}';
        DELETE FROM usage.usage_aggregates_daily WHERE workspace_id = '${WS_ID}';
        DELETE FROM usage.workspace_cost_ceilings WHERE workspace_id = '${WS_ID}';
        DELETE FROM silver.workspaces WHERE workspace_id = '${WS_ID}';
    " >/dev/null
}
trap cleanup EXIT
cleanup  # start clean

cat <<BANNER

============================================================
PHASE 0 STEP 8 TEST 10 — COST CEILING SOFT-WARN FIRE-DRILL
============================================================
Workspace:         ${WS_ID}
Monthly ceiling:   \$${CEILING_USD}
Synthetic spend:   \$${SYNTHETIC_SPEND_USD} (85% — must trip soft-warn at 80%)
============================================================
BANNER

# 1. Seed workspace + ceiling row.
$PG_PSQL_BIN -q -c "
    INSERT INTO silver.workspaces (workspace_id, name, slug)
    VALUES ('${WS_ID}', 'phase0-cost-ceiling-test',
            'phase0-cost-ceil-${WS_ID:0:8}')
    ON CONFLICT (workspace_id) DO NOTHING;

    INSERT INTO usage.workspace_cost_ceilings
        (workspace_id, monthly_ceiling_usd, soft_warn_threshold_pct,
         hard_stop_threshold_pct)
    VALUES ('${WS_ID}', ${CEILING_USD}, 80, 100)
    ON CONFLICT (workspace_id) DO UPDATE
        SET monthly_ceiling_usd = EXCLUDED.monthly_ceiling_usd,
            soft_warn_threshold_pct = EXCLUDED.soft_warn_threshold_pct,
            hard_stop_threshold_pct = EXCLUDED.hard_stop_threshold_pct,
            last_warn_sent_at = NULL,
            last_warn_pct = NULL;
" >/dev/null

# 2. Inject synthetic usage_events totalling 85% of the ceiling. Spread
#    across 5 events so the aggregate query sums them cleanly. The
#    Model Cost Summary Agent rolls up YESTERDAY's events by default
#    (it runs nightly), so we stamp `created_at` to yesterday explicitly.
$PG_PSQL_BIN -q -c "
    INSERT INTO usage.usage_events
        (workspace_id, agent_name, model_profile, model_id,
         tokens_prompt, tokens_completion, projected_cost_usd, outcome,
         created_at)
    SELECT
        '${WS_ID}'::uuid,
        'phase0-cost-ceiling-test-synthetic',
        'standard',
        'Qwen/Qwen3-14B-AWQ',
        2000, 800,
        ${SYNTHETIC_SPEND_USD}::numeric / 5,
        'success',
        (current_date - interval '1 day' + interval '12 hours')
    FROM generate_series(1, 5);
" >/dev/null

echo
echo "--- Seeded \$${SYNTHETIC_SPEND_USD} in usage_events; invoking Model Cost Summary Agent ---"

export WS_ID
export REDIS_PASSWORD="${REDIS_PASSWORD:-$(grep -E '^REDIS_PASSWORD=' "${HERE}/../.env" 2>/dev/null | cut -d= -f2- || echo)}"

outcome=$(fastapi_python_with_env WS_ID REDIS_PASSWORD -- -c "
import asyncio, asyncpg, os, sys, uuid
import redis.asyncio as aioredis
sys.path.insert(0, '/app')
from app.agents import register_runtime, AgentContext
from app.agents.phase0 import model_cost_summary_run

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
    r = await model_cost_summary_run(ctx=AgentContext(workspace_id=ws))
    await pool.close()
    await redis.aclose()
    print(r.outcome)
    return 0

sys.exit(asyncio.run(main()))
" 2>&1 | tail -1 | tr -d ' \r')

echo "  Model Cost Summary outcome: ${outcome}"

if [ "$outcome" != "success" ]; then
    echo "  [FAIL] Model Cost Summary outcome=${outcome} (expected success)"
    exit 1
fi
echo "  [PASS] outcome=success"

# 4. Assert: cost_ceiling.soft_warn audit_ledger row fired.
warn_count=$($PG_PSQL_BIN -tAc "
    SELECT count(*) FROM audit.audit_ledger
    WHERE workspace_id = '${WS_ID}'
      AND action_type = 'cost_ceiling.soft_warn'
      AND created_at > now() - interval '5 minutes';" | tr -d ' \r')

if [ "$warn_count" -lt "1" ]; then
    echo "  [FAIL] cost_ceiling.soft_warn audit row did NOT fire (expected ≥1, got ${warn_count})"
    echo "  Diagnostic — recent audit rows for workspace:"
    $PG_PSQL_BIN -c "
        SELECT action_type, payload->>'pct' AS pct
          FROM audit.audit_ledger
         WHERE workspace_id = '${WS_ID}'
         ORDER BY created_at DESC LIMIT 5;"
    exit 1
fi
echo "  [PASS] cost_ceiling.soft_warn audit row fired (${warn_count} entries)"

# 5. Verify last_warn_pct on the ceiling row is set to ≥80.
warn_pct=$($PG_PSQL_BIN -tAc "
    SELECT COALESCE(last_warn_pct::text, 'NULL')
      FROM usage.workspace_cost_ceilings
     WHERE workspace_id = '${WS_ID}';" | tr -d ' \r')

if [ "$warn_pct" = "NULL" ] || [ "$warn_pct" -lt "80" ] 2>/dev/null; then
    echo "  [FAIL] last_warn_pct = ${warn_pct} (expected ≥80)"
    exit 1
fi
echo "  [PASS] workspace_cost_ceilings.last_warn_pct = ${warn_pct}"

echo
echo "============================================================"
echo "PHASE 0 STEP 8 TEST 10 — PASS"
echo "============================================================"
exit 0
