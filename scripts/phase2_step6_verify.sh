#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_step6_verify.sh
#
# Phase 2 Step 6 done-definition — /admin/integrations dashboard.
#
#   1. IntegrationsController class loads
#   2. 2 admin/integrations routes registered (index, toggleFlag)
#   3. Inertia page TSX present (Integrations.tsx)
#   4. pgsql_activepieces connection reachable + reads `flow`
#   5. Hatchet rollup query runs cleanly against v1_runs_olap
#   6. Both Activepieces feature flags present
#   7. toggleFlag UPSERT — flips a flag and reverts; trigger captures
#      the change in feature_flag_history (R-P1-6 still wired)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7

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
PHASE 2 STEP 6 — /admin/integrations VERIFICATION
============================================================
BANNER

out=$(docker exec georag-laravel-octane php /app/scripts/_phase2_step6_check.php 2>/dev/null)

ctrl=$(echo "$out" | grep -m1 'controller_class=' | cut -d= -f2)
[ "$ctrl" = "App\\Http\\Controllers\\Admin\\IntegrationsController" ] \
    && check "IntegrationsController loads" ok \
    || check "controller load" fail "got '$ctrl'"

route_count=$(echo "$out" | grep -m1 'route_count=' | cut -d= -f2)
[ "$route_count" = "2" ] \
    && check "2 admin/integrations routes registered" ok \
    || check "route count" fail "got $route_count / 2"

inertia_present=$(docker exec georag-laravel-octane test -f /app/resources/js/Pages/Admin/Integrations.tsx \
    && echo 1 || echo 0)
[ "$inertia_present" = "1" ] \
    && check "Inertia page TSX present (Integrations.tsx)" ok \
    || check "TSX file" fail "missing"

ap_count=$(echo "$out" | grep -m1 'activepieces_flow_count=' | cut -d= -f2)
case "$ap_count" in
    ''|*ERROR*) check "pgsql_activepieces reachable" fail "$ap_count" ;;
    *)          check "pgsql_activepieces reachable + flow table queryable (n=$ap_count)" ok ;;
esac

hatchet_count=$(echo "$out" | grep -m1 'v1_runs_olap_24h=' | cut -d= -f2)
case "$hatchet_count" in
    ''|*ERROR*) check "Hatchet v1_runs_olap query" fail "$hatchet_count" ;;
    *)          check "Hatchet rollup query runs against v1_runs_olap (n=$hatchet_count)" ok ;;
esac

ap_flag_count=$(echo "$out" | grep -m1 'ap_flag_count=' | cut -d= -f2)
[ "$ap_flag_count" = "2" ] \
    && check "Both activepieces.* feature flags present" ok \
    || check "feature flags" fail "got $ap_flag_count / 2"

# 7) Flag-flip + history smoke. Use the SQL the controller would hit.
prev=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT bool_value::text FROM workspace.feature_flags
     WHERE workspace_id IS NULL
       AND flag_name = 'activepieces.public_geoscience_pull.enabled';" | tr -d ' ')
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'activepieces.public_geoscience_pull.enabled', true, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE
        SET bool_value = EXCLUDED.bool_value, updated_at = now();" >/dev/null
new=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT bool_value::text FROM workspace.feature_flags
     WHERE workspace_id IS NULL
       AND flag_name = 'activepieces.public_geoscience_pull.enabled';" | tr -d ' ')
hist_count=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM workspace.feature_flag_history
     WHERE flag_name = 'activepieces.public_geoscience_pull.enabled';" | tr -d ' ')

# Revert.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    UPDATE workspace.feature_flags
       SET bool_value = ${prev:-false}, updated_at = now()
     WHERE workspace_id IS NULL
       AND flag_name = 'activepieces.public_geoscience_pull.enabled';" >/dev/null

if [ "$new" = "true" ] && [ "$hist_count" -ge 1 ] 2>/dev/null; then
    check "Flag-flip UPSERT works + R-P1-6 history captured ($hist_count rows)" ok
else
    check "flag UPSERT + history" fail "new=$new hist=$hist_count"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"
echo
echo 'NOTE: UI rendering depends on npm run build / npm run dev.'
echo

exit $((PASS == TOTAL ? 0 : 1))
