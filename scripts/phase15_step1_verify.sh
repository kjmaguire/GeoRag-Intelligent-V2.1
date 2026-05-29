#!/usr/bin/env bash
# =============================================================================
# scripts/phase15_step1_verify.sh
#
# Phase 15 Step 1 — nightly silver MV refresh (R-P14-2). Pattern
# matches Phase 7 Step 2 (flow_jwt_key_reaper).
#
#   1. workflow.refresh_silver_agent_mvs() function present
#   2. mv_refresh_silver module loads with the expected workflow name
#   3. AI worker --list includes mv_refresh_silver
#   4. Hatchet engine registered cron '0 3 * * *' for the workflow
#   5. Direct fn invocation returns ≥1 refreshed MV
#   6. After invocation, silver.mv_collar_summary has 10 collars for
#      the test project (functional end-to-end check)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
PROJ='019d74a1-fba8-7165-9ae6-a5bf93eef97d'

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cat <<'BANNER'

============================================================
PHASE 15 STEP 1 — silver MV refresh workflow
============================================================
BANNER

# 1) SQL function
fn_present=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM information_schema.routines
     WHERE routine_schema='workflow' AND routine_name='refresh_silver_agent_mvs';" \
    | tr -d ' ')
[ "$fn_present" = "1" ] \
    && check "workflow.refresh_silver_agent_mvs() function present" ok \
    || check "function" fail "got $fn_present"

# 2) Workflow module loads
mod_ok=$(docker exec georag-hatchet-worker-ai python3 -c "
from app.hatchet_workflows.mv_refresh_silver import mv_refresh_silver
print(mv_refresh_silver.name)" 2>&1 | tail -1)
[ "$mod_ok" = "mv_refresh_silver" ] \
    && check "mv_refresh_silver module loads with expected workflow name" ok \
    || check "module" fail "$mod_ok"

# 3) AI worker --list
listed=$(docker exec georag-hatchet-worker-ai python3 -m app.hatchet_workflows.worker --list 2>&1 \
    | grep -c '^mv_refresh_silver$')
[ "$listed" = "1" ] \
    && check "AI pool --list includes mv_refresh_silver" ok \
    || check "pool registration" fail "got $listed / 1"

# 4) Cron registered. Hatchet schema: WorkflowTriggerCronRef →
# WorkflowTriggers → WorkflowVersion → Workflow.
cron_count=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc "
    SELECT count(*) FROM \"WorkflowTriggerCronRef\" c
      JOIN \"WorkflowTriggers\" t ON c.\"parentId\" = t.id
      JOIN \"WorkflowVersion\" v ON t.\"workflowVersionId\" = v.id
      JOIN \"Workflow\" w ON v.\"workflowId\" = w.id
     WHERE w.\"name\" = 'mv_refresh_silver'
       AND c.cron = '0 3 * * *'
       AND c.\"deletedAt\" IS NULL;" 2>/dev/null | tr -d ' ')
[ "${cron_count:-0}" -ge 1 ] 2>/dev/null \
    && check "Hatchet engine registered cron '0 3 * * *' for mv_refresh_silver" ok \
    || check "cron registration" fail "got count=$cron_count"

# 5) Direct fn invocation returns ≥1 MV refreshed
n_refreshed=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM workflow.refresh_silver_agent_mvs();" | tr -d ' ')
[ "${n_refreshed:-0}" -ge 1 ] 2>/dev/null \
    && check "refresh_silver_agent_mvs() refreshed ≥1 MV (got $n_refreshed)" ok \
    || check "fn invocation" fail "got $n_refreshed"

# 6) End-to-end — MV populated for the test project
mv_total=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT total_collars FROM silver.mv_collar_summary
     WHERE project_id = '$PROJ';" | tr -d ' ')
if [ "${mv_total:-0}" -ge 10 ] 2>/dev/null; then
    check "silver.mv_collar_summary populated post-refresh ($mv_total collars)" ok
else
    check "mv populated" fail "got total_collars=$mv_total (expected ≥10)"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
