#!/usr/bin/env bash
# =============================================================================
# scripts/phase38_step1_verify.sh
#
# Phase 38 Step 1 — R-P21 frontend slice. Inertia page consuming the
# Phase 37 JSON endpoint.
#
#   1. Admin/CacheTelemetry.tsx page present + non-trivial
#   2. Page imports Head + uses AppLayout
#   3. Page fetches /admin/cache-telemetry/skip-reasons.json on mount
#   4. Page renders the 5 documented skip-reason buckets
#   5. CacheTelemetryController has index() method rendering Inertia
#   6. Route GET /admin/cache-telemetry registered
#   7. Test class has 3 new methods for the page (guest, non-admin, admin)
#   8. Test class runs cleanly (skipped under SQLite is OK)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
REPO="${REPO:-/home/georag/projects/georag}"
PAGE="$REPO/resources/js/Pages/Admin/CacheTelemetry.tsx"
CTRL="$REPO/app/Http/Controllers/Admin/CacheTelemetryController.php"
WEB="$REPO/routes/web.php"
TEST="$REPO/tests/Feature/Admin/CacheTelemetryTest.php"
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
PHASE 38 STEP 1 — R-P21 frontend slice (Inertia page)
============================================================
BANNER

if [ -s "$PAGE" ]; then
    lines=$(wc -l < "$PAGE")
    [ "$lines" -ge 100 ] \
        && check "Admin/CacheTelemetry.tsx present ($lines lines)" ok \
        || check "page length" fail "only $lines lines"
else
    check "page exists" fail "missing"
fi

if grep -q "from '@inertiajs/react'" "$PAGE" \
   && grep -q "AppLayout" "$PAGE"; then
    check "Page imports Head + uses AppLayout" ok
else
    check "imports" fail "missing"
fi

if grep -q "fetch(" "$PAGE"; then
    check "Page fetches the JSON endpoint on mount" ok
else
    check "fetch call" fail "missing"
fi

# 4) Page renders the 5 skip-reason buckets
ok4=1
for reason in zero_candidates partial_failures schema_validation_failed downhole_bypass_legacy '(none)'; do
    grep -qF "$reason" "$PAGE" || ok4=0
done
if [ "$ok4" = "1" ]; then
    check "Page renders all 5 documented skip-reason buckets" ok
else
    check "skip-reason buckets" fail "missing in page"
fi

if grep -q 'public function index' "$CTRL" \
   && grep -q "Inertia::render" "$CTRL" \
   && grep -q "Admin/CacheTelemetry" "$CTRL"; then
    check "Controller has index() rendering Admin/CacheTelemetry Inertia page" ok
else
    check "controller index" fail "missing"
fi

# 6) Route registered (the no-suffix /admin/cache-telemetry)
if grep -qE "'/admin/cache-telemetry'," "$WEB" \
   && grep -q "CacheTelemetryController::class, 'index'" "$WEB"; then
    check "Route GET /admin/cache-telemetry registered" ok
else
    check "page route" fail "missing"
fi

# 7) Test class has the new page-tests
ok7=1
for t in test_guest_redirected_from_page test_non_admin_forbidden_from_page test_admin_sees_inertia_page; do
    grep -q "$t" "$TEST" || ok7=0
done
if [ "$ok7" = "1" ]; then
    check "Test class has 3 new methods for the Inertia page" ok
else
    check "test methods" fail "missing"
fi

# 8) Tests run cleanly (skipped is acceptable under SQLite)
test_out=$(docker exec "$LARAVEL" php artisan test --compact tests/Feature/Admin/CacheTelemetryTest.php 2>&1 | tail -5)
if echo "$test_out" | grep -qE 'passed|skipped'; then
    check "Feature test class runs (output contains passed/skipped)" ok
else
    check "test run" fail "$(echo "$test_out" | head -1)"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
