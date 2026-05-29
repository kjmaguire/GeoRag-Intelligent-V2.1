#!/usr/bin/env bash
# =============================================================================
# scripts/phase10_step2_verify.sh
#
# Phase 10 Step 2 done-definition — fix phase5_step1 minute-boundary
# flake (R-P9-3).
#
#   1. phase5_step1_verify.sh source contains the burst-send pattern
#      (4 POSTs back-to-back, then wait, then assert)
#   2. phase5_step1_verify.sh source contains the second-of-minute
#      alignment guard
#   3. Three consecutive standalone runs all report Result: 6/6
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=3
REPO="${REPO:-/home/georag/projects/georag}"
VERIFIER="$REPO/scripts/phase5_step1_verify.sh"

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
PHASE 10 STEP 2 — RATE-LIMIT VERIFIER FLAKE FIX
============================================================
BANNER

# 1) Burst-send pattern
if grep -q 'A_RUNS=()' "$VERIFIER" \
    && grep -q 'posted within the same minute window' "$VERIFIER"; then
    check "phase5_step1 source uses burst-send (no wait between A POSTs)" ok
else
    check "burst-send pattern" fail "pattern not present in source"
fi

# 2) Minute-alignment guard
if grep -q 'sec_remaining=$((60 - $(date -u +%-S)))' "$VERIFIER"; then
    check "Minute-alignment guard (skip-to-next-minute when <15s left)" ok
else
    check "alignment guard" fail "second-of-minute guard missing"
fi

# 3) Three consecutive runs all 6/6
runs_passed=0
for i in 1 2 3; do
    result=$(bash "$VERIFIER" 2>&1 | grep -E '^Result: ' | tail -1)
    case "$result" in
        'Result: 6 / 6 checks passed')
            runs_passed=$((runs_passed + 1))
            echo "    run #$i: 6/6 ✓"
            ;;
        *)
            echo "    run #$i: $result"
            ;;
    esac
done
[ "$runs_passed" = "3" ] \
    && check "Three consecutive standalone runs all report 6/6" ok \
    || check "consecutive runs" fail "only $runs_passed / 3 runs passed"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
