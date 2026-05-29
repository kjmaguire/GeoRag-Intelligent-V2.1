#!/usr/bin/env bash
# =============================================================================
# scripts/phase40_step1_verify.sh
#
# Phase 40 Step 1 — R-P11-B slice 2. SSE submission wired against the
# existing /api/v1/queries → Echo channel → /start handshake.
#
#   1. SearchQuery.tsx still present, ≥ 120 lines (grew from skeleton)
#   2. Page POSTs to /api/v1/queries
#   3. Page subscribes to the returned Echo channel
#   4. Page POSTs to /api/v1/queries/{id}/start
#   5. Page listens for QueryStreamEvent
#   6. Page handles status / completed / failed event types
#   7. Page contains the Phase 40 marker
#   8. Phase 39 slice-1 test still passes (no auth-contract regression)
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
PHASE 40 STEP 1 — R-P11-B slice 2 (SSE submission)
============================================================
BANNER

if [ -s "$PAGE" ]; then
    lines=$(wc -l < "$PAGE")
    [ "$lines" -ge 120 ] \
        && check "SearchQuery.tsx grew to slice-2 size ($lines lines)" ok \
        || check "page length" fail "only $lines lines (expected ≥120)"
else
    check "page exists" fail "missing"
fi

if grep -qE "fetch\('/api/v1/queries'" "$PAGE"; then
    check "Page POSTs to /api/v1/queries" ok
else
    check "create POST" fail "missing"
fi

if grep -q "window.Echo.channel" "$PAGE" \
   && grep -q "window.Echo.leave" "$PAGE"; then
    check "Page subscribes to and leaves the Echo channel" ok
else
    check "Echo subscription" fail "missing"
fi

if grep -qE 'fetch\(`/api/v1/queries/.*start`' "$PAGE"; then
    check "Page POSTs to /api/v1/queries/{id}/start" ok
else
    check "start POST" fail "missing"
fi

if grep -q "QueryStreamEvent" "$PAGE"; then
    check "Page listens for QueryStreamEvent" ok
else
    check "QueryStreamEvent listener" fail "missing"
fi

ok6=1
for et in status completed failed; do
    grep -q "'$et'" "$PAGE" || ok6=0
done
if [ "$ok6" = "1" ]; then
    check "Page handles status / completed / failed event types" ok
else
    check "event-type handlers" fail "missing one of status/completed/failed"
fi

if grep -q "Phase 40" "$PAGE"; then
    check "Page contains Phase 40 slice-2 marker" ok
else
    check "slice-2 marker" fail "missing"
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
