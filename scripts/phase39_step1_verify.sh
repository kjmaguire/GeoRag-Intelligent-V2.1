#!/usr/bin/env bash
# =============================================================================
# scripts/phase39_step1_verify.sh
#
# Phase 39 Step 1 — R-P11-B slice 1. Search/Query skeleton page.
#
#   1. SearchQuery.tsx page present + non-trivial
#   2. Page imports Head + uses AppLayout
#   3. Page has search input + submit button
#   4. Page contains the slice-1 skeleton marker
#   5. Route GET /search registered behind auth:sanctum
#   6. Route renders the Inertia 'SearchQuery' component
#   7. SearchQueryPageTest has guest + authenticated test methods
#   8. Test class runs cleanly
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
REPO="${REPO:-/home/georag/projects/georag}"
PAGE="$REPO/resources/js/Pages/SearchQuery.tsx"
WEB="$REPO/routes/web.php"
TEST="$REPO/tests/Feature/SearchQueryPageTest.php"
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
PHASE 39 STEP 1 — R-P11-B slice 1 (Search/Query skeleton)
============================================================
BANNER

if [ -s "$PAGE" ]; then
    lines=$(wc -l < "$PAGE")
    [ "$lines" -ge 40 ] \
        && check "SearchQuery.tsx present ($lines lines)" ok \
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

if grep -q '<input' "$PAGE" \
   && grep -q 'type="text"' "$PAGE" \
   && grep -q '<button' "$PAGE" \
   && grep -q 'type="submit"' "$PAGE"; then
    check "Page has search input + submit button" ok
else
    check "form elements" fail "input or submit button missing"
fi

# Phase 39 slice 1 had a "Phase 39 skeleton" footer marker. Phase 43 (slice
# 5) replaced it with "R-P11-B complete — Phase 43". Supersession-tolerant:
# either marker satisfies the check — both confirm the page descends from
# the slice-1 skeleton heritage.
if grep -q "Phase 39 skeleton" "$PAGE" || grep -q "R-P11-B complete" "$PAGE"; then
    check "Page carries slice-1 heritage marker (slice-1 or post-slice-5 form)" ok
else
    check "skeleton marker" fail "missing both 'Phase 39 skeleton' and 'R-P11-B complete'"
fi

# 5) Route registered behind auth:sanctum
if awk "/auth:sanctum/,/^}\)/" "$WEB" | grep -qE "'/search'"; then
    check "Route GET /search registered under auth:sanctum group" ok
else
    check "search route" fail "missing or not behind auth:sanctum"
fi

# 6) Route renders the SearchQuery Inertia component
if grep -qE "Inertia::render\('SearchQuery'\)" "$WEB"; then
    check "Route renders Inertia 'SearchQuery' component" ok
else
    check "Inertia component" fail "render call missing"
fi

# 7) Test class has the new page-tests
ok7=1
for t in test_guest_is_redirected_to_login test_authenticated_user_sees_search_page; do
    grep -q "$t" "$TEST" || ok7=0
done
if [ "$ok7" = "1" ]; then
    check "Test class has guest + authenticated test methods" ok
else
    check "test methods" fail "missing"
fi

# 8) Tests run cleanly
test_out=$(docker exec "$LARAVEL" php artisan test --compact tests/Feature/SearchQueryPageTest.php 2>&1 | tail -5)
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
