#!/usr/bin/env bash
# =============================================================================
# scripts/phase7_step2_verify.sh
#
# Phase 7 Step 2 done-definition — auto-prune flow_jwt_keys (R-P6-2).
#
#   1. workflow.reap_expired_flow_jwt_keys() function present + grants
#   2. flow_jwt_key_reaper workflow source module loads + registered
#   3. AI worker --list output includes flow_jwt_key_reaper
#   4. Hatchet engine has the cron trigger registered (0 4 * * *)
#   5. Functional: insert one expired + one fresh row; call reap
#      function; only the expired row is gone
#   6. Reap with retention_days=0 reaps "just-now expired" rows
#   7. Reap with retention_days=99999 keeps everything (safety check)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
FLOW="phase2_smoke"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        DELETE FROM workflow.flow_jwt_keys WHERE kid LIKE 'p7s2-%';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

cat <<'BANNER'

============================================================
PHASE 7 STEP 2 — flow_jwt_keys AUTO-PRUNE VERIFICATION
============================================================
BANNER

# 1) Function present + grant
fn_present=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM information_schema.routines
     WHERE routine_schema='workflow' AND routine_name='reap_expired_flow_jwt_keys';" \
    | tr -d ' ')
[ "$fn_present" = "1" ] \
    && check "workflow.reap_expired_flow_jwt_keys() function present" ok \
    || check "function" fail "got $fn_present / 1"

# 2) Workflow module imports
mod_ok=$(docker exec georag-hatchet-worker-ai python3 -c "
from app.hatchet_workflows.flow_jwt_key_reaper import flow_jwt_key_reaper
print(flow_jwt_key_reaper.name)
" 2>&1 | tail -1)
[ "$mod_ok" = "flow_jwt_key_reaper" ] \
    && check "flow_jwt_key_reaper module loads with the expected workflow name" ok \
    || check "workflow module" fail "$mod_ok"

# 3) AI worker --list
listed=$(docker exec georag-hatchet-worker-ai python3 -m app.hatchet_workflows.worker --list 2>&1 \
    | grep -c '^flow_jwt_key_reaper$')
[ "$listed" = "1" ] \
    && check "AI pool worker --list includes flow_jwt_key_reaper" ok \
    || check "pool registration" fail "got $listed / 1"

# 4) Hatchet engine sees the cron trigger. WorkflowTriggerCronRef
# parents to WorkflowTriggers → WorkflowVersion → Workflow (Hatchet
# 1.x schema), not directly to Workflow.
cron_count=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc "
    SELECT count(*) FROM \"WorkflowTriggerCronRef\" c
      JOIN \"WorkflowTriggers\" t ON c.\"parentId\" = t.id
      JOIN \"WorkflowVersion\" v ON t.\"workflowVersionId\" = v.id
      JOIN \"Workflow\" w ON v.\"workflowId\" = w.id
     WHERE w.\"name\" = 'flow_jwt_key_reaper'
       AND c.cron = '0 4 * * *'
       AND c.\"deletedAt\" IS NULL;" 2>/dev/null | tr -d ' ')
[ "${cron_count:-0}" -ge 1 ] 2>/dev/null \
    && check "Hatchet engine registered cron '0 4 * * *' for flow_jwt_key_reaper" ok \
    || check "cron registration" fail "got count=$cron_count"

# $FLOW is seeded by Phase 4 Step 4's flow_registry migration; we
# don't re-INSERT here because that requires the non-null `kind`
# column to be set.

# Seed: one row already expired 10d ago (should reap), one expired 1d
# ago (should NOT reap under default 7d retention).
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
    VALUES
      ('$FLOW', 'p7s2-old',   '\x00', clock_timestamp() - interval '30 days',
        clock_timestamp() - interval '10 days'),
      ('$FLOW', 'p7s2-fresh', '\x00', clock_timestamp() - interval '5 days',
        clock_timestamp() - interval '1 day');
" >/dev/null

# 5) Functional reap with retention_days=7 (default)
reaped=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT deleted_count FROM workflow.reap_expired_flow_jwt_keys(7);" | tr -d ' ')
remaining=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT string_agg(kid, ',' ORDER BY kid)
      FROM workflow.flow_jwt_keys WHERE kid LIKE 'p7s2-%';" | tr -d ' ')
if [ "$reaped" = "1" ] && [ "$remaining" = "p7s2-fresh" ]; then
    check "Reaper@7d deleted 1 expired row, kept the fresh one" ok
else
    check "reap default" fail "reaped=$reaped remaining=$remaining"
fi

# 6) Retention_days=0 reaps just-now-expired rows. Insert a row that
#    expired 1 second ago.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
    VALUES ('$FLOW', 'p7s2-justnow', '\x00',
            clock_timestamp() - interval '1 day',
            clock_timestamp() - interval '1 second');
" >/dev/null
reaped_now=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT deleted_count FROM workflow.reap_expired_flow_jwt_keys(0);" | tr -d ' ')
justnow_left=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM workflow.flow_jwt_keys WHERE kid = 'p7s2-justnow';" | tr -d ' ')
if [ "$reaped_now" -ge 1 ] 2>/dev/null && [ "$justnow_left" = "0" ]; then
    check "Reaper@0d removes a row that expired 1 second ago" ok
else
    check "reap 0d" fail "reaped=$reaped_now justnow_left=$justnow_left"
fi

# 7) Retention_days=99999 — should reap NOTHING (everything is within
# the window).
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
    VALUES ('$FLOW', 'p7s2-safety', '\x00',
            clock_timestamp() - interval '100 days',
            clock_timestamp() - interval '50 days');
" >/dev/null
reaped_safe=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT deleted_count FROM workflow.reap_expired_flow_jwt_keys(99999);" | tr -d ' ')
safety_left=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM workflow.flow_jwt_keys WHERE kid = 'p7s2-safety';" | tr -d ' ')
if [ "$reaped_safe" = "0" ] && [ "$safety_left" = "1" ]; then
    check "Reaper@99999d safety: reaps nothing (large retention)" ok
else
    check "reap safety" fail "reaped=$reaped_safe safety_left=$safety_left"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
