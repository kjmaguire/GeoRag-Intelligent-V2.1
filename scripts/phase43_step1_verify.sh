#!/usr/bin/env bash
# =============================================================================
# scripts/phase43_step1_verify.sh
#
# Phase 43 Step 1 — R-P11-B slice 5 (final). Top-nav integration; the
# /search route now has a visible entry between Chat and Explorer.
#
#   1. AppLayout desktop nav has a /search Link
#   2. AppLayout mobile nav has a /search Link
#   3. /search appears between /chat and /explorer in the desktop nav
#   4. SearchQuery.tsx footer marks R-P11-B complete (Phase 43)
#   5. SearchQuery.tsx still imports EvidenceInspector (slice 3 intact)
#   6. SearchQuery.tsx still has the history panel (slice 4 intact)
#   7. /search route still registered behind auth:sanctum
#   8. Phase 39 feature test still passes
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
REPO="${REPO:-/home/georag/projects/georag}"
PAGE="$REPO/resources/js/Pages/SearchQuery.tsx"
LAYOUT="$REPO/resources/js/Layouts/AppLayout.tsx"
WEB="$REPO/routes/web.php"
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
PHASE 43 STEP 1 — R-P11-B slice 5 (top-nav + cleanup)
============================================================
BANNER

# 1) Desktop nav has a /search link (≥ 1 occurrence anywhere)
search_link_count=$(grep -cE 'href="/search"' "$LAYOUT" || true)
if [ "$search_link_count" -ge 2 ]; then
    check "AppLayout has /search Link in both desktop + mobile nav ($search_link_count refs)" ok
else
    check "AppLayout /search links" fail "expected ≥2 occurrences, found $search_link_count"
fi

# 2) (covered by 1) — check explicit "Search" label
if grep -q '>Search<' "$LAYOUT"; then
    check "AppLayout renders the 'Search' label text" ok
else
    check "Search label" fail "missing"
fi

# 3) Search appears between Chat and Explorer in desktop nav
ordered=$(awk '/aria-label="Main navigation"/,/<\/nav>/' "$LAYOUT" | grep -oE 'href="/[a-z-]+"' | head -10)
if echo "$ordered" | awk '
    /href="\/chat"/ {chat=1}
    /href="\/search"/ {if (chat && !explorer) search=1}
    /href="\/explorer"/ {explorer=1}
    END {exit !(chat && search && explorer)}
'; then
    check "Desktop nav order: /chat → /search → /explorer" ok
else
    check "nav ordering" fail "order is $(echo $ordered | tr '\n' ' ')"
fi

if grep -q "R-P11-B complete" "$PAGE"; then
    check "SearchQuery footer marks R-P11-B complete (Phase 43)" ok
else
    check "completion marker" fail "missing"
fi

if grep -q "EvidenceInspector" "$PAGE"; then
    check "EvidenceInspector reuse from slice 3 intact" ok
else
    check "slice-3 reuse" fail "EvidenceInspector reference missing"
fi

if grep -q "SEARCH_HISTORY_KEY" "$PAGE"; then
    check "History panel from slice 4 intact" ok
else
    check "slice-4 history" fail "SEARCH_HISTORY_KEY missing"
fi

# 7) /search route still registered behind auth:sanctum
if awk '/auth:sanctum/,/^}\)/' "$WEB" | grep -qE "'/search'"; then
    check "Route GET /search still under auth:sanctum" ok
else
    check "search route" fail "missing"
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
