#!/usr/bin/env bash
# Phase 17 Step 3 — re-baseline doc.
set -uo pipefail
PASS=0; TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase17_golden_baseline_v2.md"

check() {
    if [ "$2" = ok ]; then echo "  [PASS] $1"; PASS=$((PASS+1)); else echo "  [FAIL] $1 — $3"; fi
}

echo
echo "PHASE 17 STEP 3 — GOLDEN BASELINE V2"
echo "============================================================"

[ -s "$DOC" ] && check "Baseline v2 doc present" ok || check "doc" fail "missing"

if grep -q 'Phase 17 peak' "$DOC" && grep -q '15' "$DOC"; then
    check "Doc records Phase 17 peak (15/31)" ok
else
    check "peak" fail "missing"
fi

if grep -q '20' "$DOC" && grep -q '360.8' "$DOC" && grep -q 'uranium' "$DOC"; then
    check "Doc captures the three direct unlocks (20/360.8/uranium)" ok
else
    check "unlocks" fail "missing"
fi

if grep -q 'R-P14-3' "$DOC" && grep -q 'warm-run' "$DOC"; then
    check "Doc flags R-P14-3 warm-run drop for Phase 18+ investigation" ok
else
    check "carry-over" fail "missing"
fi

# Floor stays at 2 — phase11_step2 + step4 verifiers should still pass
if grep -qE 'pass count must be . 2' "$REPO/docs/phase11_golden_baseline.md"; then
    check "Phase 11 baseline doc still references ≥2 floor (unchanged)" ok
else
    check "floor unchanged" fail "Phase 11 baseline doc lost the floor"
fi

echo
echo "Result: $PASS / $TOTAL checks passed"
exit $((PASS == TOTAL ? 0 : 1))
