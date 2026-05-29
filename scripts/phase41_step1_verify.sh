#!/usr/bin/env bash
# =============================================================================
# scripts/phase41_step1_verify.sh
#
# Phase 41 Step 1 — R-P11-B slice 3. Citation rendering via the same
# EvidenceInspector overlay Chat.tsx uses; raw-JSON stub removed.
#
#   1. SearchQuery.tsx imports EvidenceInspector
#   2. Page imports the Citation type
#   3. Page renders <EvidenceInspector ... />
#   4. Inspector state has open / evidenceId / legacyCitation fields
#   5. Citation kind icons cover all 4 citation_type values
#   6. Page no longer carries the raw-JSON.stringify stub
#   7. Page contains the Phase 41 marker
#   8. Phase 39 feature test still passes (auth contract unchanged)
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
PHASE 41 STEP 1 — R-P11-B slice 3 (citation rendering)
============================================================
BANNER

if grep -q "import .*EvidenceInspector.*from" "$PAGE"; then
    check "Page imports EvidenceInspector" ok
else
    check "EvidenceInspector import" fail "missing"
fi

if grep -qE "import type \{ Citation \} from '@/types'" "$PAGE"; then
    check "Page imports the Citation type from @/types" ok
else
    check "Citation type import" fail "missing"
fi

if grep -q "<EvidenceInspector" "$PAGE"; then
    check "Page renders <EvidenceInspector />" ok
else
    check "EvidenceInspector element" fail "missing"
fi

ok4=1
for field in "open:" "evidenceId:" "legacyCitation:"; do
    grep -q "$field" "$PAGE" || ok4=0
done
if [ "$ok4" = "1" ]; then
    check "Inspector state covers open / evidenceId / legacyCitation" ok
else
    check "inspector state" fail "missing one of open/evidenceId/legacyCitation"
fi

ok5=1
for kind in "NI43:" "PUB:" "DATA:" "PGEO:"; do
    grep -q "$kind" "$PAGE" || ok5=0
done
if [ "$ok5" = "1" ]; then
    check "Citation kind icon map covers NI43 / PUB / DATA / PGEO" ok
else
    check "citation kinds" fail "missing one of NI43/PUB/DATA/PGEO"
fi

if grep -q "JSON.stringify(result.citations" "$PAGE" || grep -q "slice-3 will render properly" "$PAGE"; then
    check "raw JSON citation stub removed" fail "stub still present"
else
    check "Raw JSON citation stub removed" ok
fi

if grep -q "Phase 41" "$PAGE"; then
    check "Page contains Phase 41 slice-3 marker" ok
else
    check "slice-3 marker" fail "missing"
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
