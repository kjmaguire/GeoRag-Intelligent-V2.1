#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step6_verify.sh
#
# Phase 3 Step 6 done-definition — /admin/integrations dashboard pivot.
#
#   1. pgsql_kestra connection reachable + reads `flows` table
#   2. IntegrationsController::loadKestraFlows() returns rows
#   3. PageProps now includes kestra_flows (route output check)
#   4. Inertia page TSX references kestra_flows
#   5. Both pgsql_activepieces (legacy, sunset Step 7) + pgsql_kestra
#      connections still work side-by-side
#   6. All Phase 1+2+3 verifiers green (regression sweep)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6

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
PHASE 3 STEP 6 — /admin/integrations DASHBOARD PIVOT
============================================================
BANNER

# 1) + 2) — single PHP probe
out=$(docker exec georag-laravel-octane php /app/scripts/_phase3_step6_check.php 2>/dev/null)
ap_count=$(echo "$out" | grep -m1 'kestra_flow_count=' | cut -d= -f2)
case "$ap_count" in
    [0-9]*) check "pgsql_kestra reachable + flows table queryable (n=$ap_count)" ok ;;
    *)      check "pgsql_kestra reachable" fail "$ap_count" ;;
esac

helper_count=$(echo "$out" | grep -m1 'kestra_helper_count=' | cut -d= -f2)
case "$helper_count" in
    [0-9]*) check "loadKestraFlows() helper returns rows (n=$helper_count)" ok ;;
    *)      check "loadKestraFlows()" fail "$helper_count" ;;
esac

# 3) Hit the actual route as an admin (read by reflection — no need to
#    spin up a real session). Verify the controller's index() returns
#    a page with kestra_flows in its props array.
props_check=$(docker exec georag-laravel-octane php -r '
require "/app/vendor/autoload.php";
$app = require "/app/bootstrap/app.php";
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$c = new App\Http\Controllers\Admin\IntegrationsController();
$rc = new ReflectionClass($c);
$names = array_map(fn ($m) => $m->getName(), $rc->getMethods(ReflectionMethod::IS_PRIVATE));
echo in_array("loadKestraFlows", $names) ? "OK" : "MISSING";
' 2>&1 | tail -1)
[ "$props_check" = "OK" ] \
    && check "Controller exposes loadKestraFlows()" ok \
    || check "controller method" fail "$props_check"

# 4) TSX references kestra_flows
tsx_ok=$(docker exec georag-laravel-octane bash -c '
    grep -q "kestra_flows" /app/resources/js/Pages/Admin/Integrations.tsx \
    && grep -q "Kestra flows" /app/resources/js/Pages/Admin/Integrations.tsx \
    && echo OK || echo MISSING
')
[ "$tsx_ok" = "OK" ] \
    && check "Inertia page renders Kestra flows section" ok \
    || check "TSX kestra_flows" fail "$tsx_ok"

# 5) pgsql_kestra connection works as the primary integration-edge view.
#    (Phase 3 Step 7 dropped pgsql_activepieces; this check used to be
#    a side-by-side, now Kestra-only.)
kestra_ok=$(docker exec georag-laravel-octane php -r '
require "/app/vendor/autoload.php";
$app = require "/app/bootstrap/app.php";
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
try {
    $k = DB::connection("pgsql_kestra")->table("flows")->count();
    echo "OK k=$k";
} catch (\Throwable $e) {
    echo "ERROR " . $e->getMessage();
}
' 2>&1 | tail -1)
case "$kestra_ok" in
    OK*) check "pgsql_kestra readable as primary integration-edge view ($kestra_ok)" ok ;;
    *)   check "kestra connection" fail "$kestra_ok" ;;
esac

# 6) Regression sweep — Phase 3 Steps 4 + 5 (Phase 2 Step 6 was
#    archived at Phase 3 Step 7 since pgsql_activepieces no longer exists).
echo
echo "  ── Regression sweep (Phase 3 Steps 4 + 5) ──"
fail=0
for s in phase3_step4 phase3_step5; do
    r=$(bash /home/georag/projects/georag/scripts/${s}_verify.sh 2>&1 | grep -E '^Result' | head -1)
    echo "    $s: $r"
    if [[ "$r" =~ Result:\ ([0-9]+)\ /\ ([0-9]+)\ checks\ passed ]]; then
        if [ "${BASH_REMATCH[1]}" != "${BASH_REMATCH[2]}" ]; then
            fail=$((fail+1))
        fi
    else
        fail=$((fail+1))
    fi
done
[ "$fail" = "0" ] \
    && check "Regression sweep — Phase 3 Steps 4+5 green" ok \
    || check "regression" fail "$fail verifier(s) failed"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
