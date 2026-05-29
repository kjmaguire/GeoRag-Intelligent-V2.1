#!/usr/bin/env bash
# =============================================================================
# scripts/phase19_master_sweep.sh
#
# Run every verifier from Phase 0 → Phase 19 and report cumulative
# pass count. Exit 0 only at 100% (excluding documented Phase 4/9
# carry-over non-greens — see Phase 18 handoff section 6).
# =============================================================================

set -uo pipefail

REPO="${REPO:-/home/georag/projects/georag}"
cd "$REPO"

VERIFIERS=(
    scripts/phase0_step1_verify.sh
    scripts/phase0_step2_verify.sh
    scripts/phase0_step3_verify.sh
    scripts/phase0_step4_verify.sh
    scripts/phase1_step1_verify.sh
    scripts/phase1_step2_verify.sh
    scripts/phase1_step3_verify.sh
    scripts/phase1_step4_verify.sh
    scripts/phase2_step1_verify.sh
    scripts/phase2_step2_verify.sh
    scripts/phase2_step3_verify.sh
    scripts/phase2_step4_verify.sh
    scripts/phase3_step1_verify.sh
    scripts/phase3_step2_verify.sh
    scripts/phase3_step3_verify.sh
    scripts/phase3_step4_verify.sh
    scripts/phase4_step1_verify.sh
    scripts/phase4_step2_verify.sh
    scripts/phase4_step3_verify.sh
    scripts/phase4_step4_verify.sh
    scripts/phase4_step5_verify.sh
    scripts/phase4_step6_verify.sh
    scripts/phase4_step7_verify.sh
    scripts/phase5_step1_verify.sh
    scripts/phase5_step2_verify.sh
    scripts/phase5_step3_verify.sh
    scripts/phase6_step1_verify.sh
    scripts/phase6_step2_verify.sh
    scripts/phase6_step3_verify.sh
    scripts/phase7_step1_verify.sh
    scripts/phase7_step2_verify.sh
    scripts/phase7_step3_verify.sh
    scripts/phase8_step1_verify.sh
    scripts/phase8_step2_verify.sh
    scripts/phase8_step3_verify.sh
    scripts/phase9_step1_verify.sh
    scripts/phase9_step2_verify.sh
    scripts/phase9_step3_verify.sh
    scripts/phase10_step1_verify.sh
    scripts/phase10_step2_verify.sh
    scripts/phase10_step3_verify.sh
    scripts/phase11_step1_verify.sh
    scripts/phase11_step2_verify.sh
    scripts/phase11_step3_verify.sh
    scripts/phase11_step4_verify.sh
    scripts/phase12_step1_verify.sh
    scripts/phase12_step2_verify.sh
    scripts/phase12_step3_verify.sh
    scripts/phase13_step1_verify.sh
    scripts/phase13_step2_verify.sh
    scripts/phase13_step3_verify.sh
    scripts/phase13_step4_verify.sh
    scripts/phase14_step1_verify.sh
    scripts/phase14_step2_verify.sh
    scripts/phase14_step3_verify.sh
    scripts/phase15_step1_verify.sh
    scripts/phase15_step2_verify.sh
    scripts/phase15_step3_verify.sh
    scripts/phase16_step1_verify.sh
    scripts/phase16_step2_verify.sh
    scripts/phase17_step1_verify.sh
    scripts/phase17_step2_verify.sh
    scripts/phase17_step3_verify.sh
    scripts/phase18_step1_verify.sh
    scripts/phase18_step2_verify.sh
    scripts/phase18_step3_verify.sh
    scripts/phase18_step4_verify.sh
    scripts/phase18_step5_verify.sh
    scripts/phase19_step1_verify.sh
    scripts/phase19_step2_verify.sh
    scripts/phase19_step3_verify.sh
    scripts/phase19_step4_verify.sh
)

TOTAL_VERIFIERS=0
PASS_VERIFIERS=0
FAIL_VERIFIERS=()
SUM_CHECKS=0
SUM_TOTAL=0

for v in "${VERIFIERS[@]}"; do
    if [ ! -x "$v" ]; then
        if [ -f "$v" ]; then
            chmod +x "$v" 2>/dev/null || true
        else
            continue
        fi
    fi
    TOTAL_VERIFIERS=$((TOTAL_VERIFIERS+1))
    out=$(bash "$v" 2>&1)
    line=$(echo "$out" | grep -E '^Result: [0-9]+ / [0-9]+' | tail -1)
    if [ -n "$line" ]; then
        p=$(echo "$line" | awk '{print $2}')
        t=$(echo "$line" | awk '{print $4}')
        SUM_CHECKS=$((SUM_CHECKS + p))
        SUM_TOTAL=$((SUM_TOTAL + t))
        if [ "$p" = "$t" ]; then
            PASS_VERIFIERS=$((PASS_VERIFIERS+1))
            printf "  [OK]   %-50s %s\n" "$(basename "$v")" "$p/$t"
        else
            FAIL_VERIFIERS+=("$(basename "$v") ($p/$t)")
            printf "  [FAIL] %-50s %s\n" "$(basename "$v")" "$p/$t"
        fi
    else
        FAIL_VERIFIERS+=("$(basename "$v") (no result line)")
        printf "  [????] %-50s (no result line)\n" "$(basename "$v")"
    fi
done

echo
echo "============================================================"
echo "PHASE 0 → 19 MASTER SWEEP"
echo "============================================================"
echo "Verifiers: $PASS_VERIFIERS / $TOTAL_VERIFIERS green"
echo "Checks:    $SUM_CHECKS / $SUM_TOTAL across all verifiers"
if [ "${#FAIL_VERIFIERS[@]}" -gt 0 ]; then
    echo
    echo "Failing verifiers:"
    for f in "${FAIL_VERIFIERS[@]}"; do echo "  - $f"; done
fi
echo "============================================================"

exit $((PASS_VERIFIERS == TOTAL_VERIFIERS ? 0 : 1))
