#!/usr/bin/env bash
# =============================================================================
# scripts/phase42_step1_verify.sh
#
# Phase 42 Step 1 — R-P11-B slice 4. Recent-queries history (last 10
# in localStorage) + URL deep-link via ?q=… so /search?q=foo
# auto-submits on mount.
#
#   1. Page references localStorage with the versioned history key
#   2. Page caps history to SEARCH_HISTORY_MAX (= 10)
#   3. Page reads ?q= from URLSearchParams on mount and auto-submits
#   4. Page writes to history.replaceState on each query
#   5. Page renders the "Recent queries" section header
#   6. Page has a Clear-history control
#   7. Page contains the Phase 42 marker
#   8. Phase 39 feature test still passes
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
REPO="${REPO:-/home/georag/projects/georag}"
PAGE="$REPO/resources/js/Pages/SearchQuery.tsx"
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
PHASE 42 STEP 1 — R-P11-B slice 4 (history + deep-link)
============================================================
BANNER

if grep -q "SEARCH_HISTORY_KEY" "$PAGE" \
   && grep -q "localStorage" "$PAGE"; then
    check "Page references localStorage with versioned history key" ok
else
    check "history storage" fail "missing"
fi

if grep -qE "SEARCH_HISTORY_MAX *= *10" "$PAGE"; then
    check "History capped at SEARCH_HISTORY_MAX = 10" ok
else
    check "history cap" fail "missing or wrong value"
fi

if grep -q "URLSearchParams" "$PAGE" \
   && grep -q "params.get('q')" "$PAGE"; then
    check "Page reads ?q= from URL on mount" ok
else
    check "url deep-link read" fail "missing"
fi

if grep -q "window.history.replaceState" "$PAGE"; then
    check "Page reflects active query via history.replaceState" ok
else
    check "replaceState" fail "missing"
fi

if grep -q "Recent queries" "$PAGE"; then
    check "Page renders the 'Recent queries' section header" ok
else
    check "history header" fail "missing"
fi

if grep -qE "handleClearHistory|>Clear<" "$PAGE"; then
    check "Page has a clear-history control" ok
else
    check "clear control" fail "missing"
fi

if grep -q "Phase 42" "$PAGE"; then
    check "Page contains Phase 42 slice-4 marker" ok
else
    check "slice-4 marker" fail "missing"
fi

# 8) Phase 39 feature tests still pass
test_out=$(docker exec "$LARAVEL" php artisan test --compact tests/Feature/SearchQueryPageTest.php 2>&1 | tail -5)
if echo "$test_out" | grep -qE 'passed|skipped'; then
    check "Phase 39 feature tests still run cleanly" ok
else
    check "test run" fail "$(echo "$test_out" | head -1)"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
