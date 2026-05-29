#!/usr/bin/env bash
# =============================================================================
# scripts/phase37_step1_verify.sh
#
# Phase 37 Step 1 — R-P21-CACHE-TELEMETRY-DASHBOARD backend slice.
# Surfaces silver.answer_runs cache aggregations via a Laravel admin
# JSON endpoint.
#
#   1. CacheTelemetryController.php present + non-trivial
#   2. Controller declares skipReasons() method with admin authorization
#   3. Controller returns the documented JSON shape
#   4. Route `/admin/cache-telemetry/skip-reasons.json` registered
#   5. Route is under auth:sanctum middleware group
#   6. Feature test file exists with all 4 documented assertions
#   7. Feature test runs (passes or skipped on non-PG driver)
#   8. Underlying column silver.answer_runs.cache_skipped_reason exists
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
REPO="${REPO:-/home/georag/projects/georag}"
CTRL="$REPO/app/Http/Controllers/Admin/CacheTelemetryController.php"
WEB="$REPO/routes/web.php"
TEST="$REPO/tests/Feature/Admin/CacheTelemetryTest.php"
PG=georag-postgresql
LARAVEL=georag-laravel-octane

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
PHASE 37 STEP 1 — CacheTelemetry endpoint (R-P21-* slice 1)
============================================================
BANNER

if [ -s "$CTRL" ]; then
    lines=$(wc -l < "$CTRL")
    [ "$lines" -ge 100 ] \
        && check "Controller present ($lines lines)" ok \
        || check "controller length" fail "only $lines lines"
else
    check "controller exists" fail "missing"
fi

if grep -q 'public function skipReasons' "$CTRL" \
   && grep -q "authorize.*'admin'" "$CTRL"; then
    check "Controller has skipReasons() with admin authorization" ok
else
    check "method + auth" fail "missing"
fi

if grep -q "'window_hours'" "$CTRL" \
   && grep -q "'totals'" "$CTRL" \
   && grep -q "'skipped_reasons'" "$CTRL" \
   && grep -q "'last_hour'" "$CTRL"; then
    check "Controller returns documented JSON shape (4 top-level keys)" ok
else
    check "json shape" fail "key missing"
fi

if grep -q "admin/cache-telemetry/skip-reasons.json" "$WEB" \
   && grep -q "CacheTelemetryController" "$WEB"; then
    check "Route /admin/cache-telemetry/skip-reasons.json registered" ok
else
    check "route" fail "missing"
fi

# 5) Route inside auth:sanctum group — confirm by checking the route line
# appears after the auth:sanctum middleware open block.
auth_line=$(grep -n "auth:sanctum" "$WEB" | head -1 | cut -d: -f1)
route_line=$(grep -n "admin/cache-telemetry/skip-reasons.json" "$WEB" | head -1 | cut -d: -f1)
if [ -n "$auth_line" ] && [ -n "$route_line" ] && [ "$route_line" -gt "$auth_line" ]; then
    check "Route registered under auth:sanctum group (line $route_line > auth line $auth_line)" ok
else
    check "auth scope" fail "auth_line=$auth_line route_line=$route_line"
fi

if [ -s "$TEST" ]; then
    asserts=$(grep -c 'public function test_' "$TEST")
    if [ "${asserts:-0}" -ge 4 ] 2>/dev/null; then
        check "Feature test file has 4 test methods (got $asserts)" ok
    else
        check "test methods" fail "got $asserts"
    fi
else
    check "test file" fail "missing"
fi

# 7) Test file runs (skipped on SQLite is OK — RequiresPostgres trait).
test_out=$(docker exec "$LARAVEL" php artisan test --compact tests/Feature/Admin/CacheTelemetryTest.php 2>&1 | tail -5)
if echo "$test_out" | grep -qE 'passed|skipped'; then
    check "Feature test runs (output contains passed/skipped)" ok
else
    check "test run" fail "$(echo "$test_out" | head -1)"
fi

# 8) Underlying column exists
col=$(docker exec "$PG" psql -U georag -d georag -tAc \
    "SELECT count(*) FROM information_schema.columns WHERE table_schema='silver' AND table_name='answer_runs' AND column_name='cache_skipped_reason';" | tr -d ' ')
if [ "$col" = "1" ]; then
    check "silver.answer_runs.cache_skipped_reason column exists (Phase 30)" ok
else
    check "column" fail "not present"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
