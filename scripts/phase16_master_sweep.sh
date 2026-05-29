#!/usr/bin/env bash
# Phase 0 → Phase 16 master regression sweep.
set -uo pipefail

REPO="${REPO:-/home/georag/projects/georag}"

SCRIPTS=(
    phase0_step1_verify.sh phase0_step2_verify.sh phase0_step3_verify.sh
    phase0_step4_verify.sh phase0_step5_verify.sh phase0_step6_verify.sh
    phase1_step2_verify.sh phase1_step7_verify.sh
    phase2_step7_verify.sh
    phase3_step1_verify.sh phase3_step2_verify.sh phase3_step3_verify.sh
    phase3_step4_verify.sh phase3_step5_verify.sh phase3_step6_verify.sh
    phase3_step7_verify.sh
    phase4_step1_verify.sh phase4_step2_verify.sh phase4_step3_verify.sh
    phase4_step4_verify.sh phase4_step5_verify.sh phase4_step6_verify.sh
    phase4_step7_verify.sh
    phase5_step1_verify.sh phase5_step2_verify.sh phase5_step3_verify.sh
    phase5_step4_verify.sh
    phase6_step1_verify.sh phase6_step2_verify.sh phase6_step3_verify.sh
    phase7_step1_verify.sh phase7_step2_verify.sh phase7_step3_verify.sh
    phase7_step4_verify.sh
    phase8_step1_verify.sh phase8_step2_verify.sh phase8_step3_verify.sh
    phase8_step4_verify.sh
    phase9_step1_verify.sh phase9_step2_verify.sh phase9_step3_verify.sh
    phase10_step1_verify.sh phase10_step2_verify.sh phase10_step3_verify.sh
    phase10_step4_verify.sh
    phase11_step1_verify.sh phase11_step2_verify.sh phase11_step3_verify.sh
    phase11_step4_verify.sh phase11_step5_verify.sh
    phase12_step1_verify.sh phase12_step2_verify.sh phase12_step3_verify.sh
    phase12_step4_verify.sh
    phase13_step1_verify.sh phase13_step2_verify.sh phase13_step3_verify.sh
    phase13_step4_verify.sh
    phase14_step1_verify.sh phase14_step2_verify.sh phase14_step3_verify.sh
    phase15_step1_verify.sh phase15_step2_verify.sh
    phase16_step1_verify.sh phase16_step2_verify.sh
)

echo
echo "============================================================"
echo "PHASE 0 → PHASE 16 MASTER REGRESSION SWEEP"
echo "============================================================"

GRAND_PASS=0; GRAND_TOTAL=0; FAILED=()
for s in "${SCRIPTS[@]}"; do
    path="$REPO/scripts/$s"
    [ -f "$path" ] || { echo "  $s  →  MISSING"; FAILED+=("$s:missing"); continue; }
    chmod +x "$path" 2>/dev/null || true
    out=$(COMPOSE_NETWORK=georag bash "$path" 2>&1 || true)
    line=$(echo "$out" | grep -E '^Result: ' | tail -1)
    if [ -z "$line" ]; then echo "  $s  →  NO RESULT LINE"; FAILED+=("$s:no-result"); continue; fi
    p=$(echo "$line" | awk '{print $2}')
    t=$(echo "$line" | awk '{print $4}')
    GRAND_PASS=$((GRAND_PASS + p)); GRAND_TOTAL=$((GRAND_TOTAL + t))
    if [ "$p" = "$t" ]; then echo "  $s  →  $p / $t  ✓"; else echo "  $s  →  $p / $t  ✗"; FAILED+=("$s:$p/$t"); fi
done

echo "------------------------------------------------------------"
echo "Grand total: ${GRAND_PASS} / ${GRAND_TOTAL} (${#SCRIPTS[@]} verifiers)"
if [ "${#FAILED[@]}" -gt 0 ]; then echo; echo "FAILED:"; for f in "${FAILED[@]}"; do echo "  - $f"; done; fi
echo "============================================================"

exit $((GRAND_PASS == GRAND_TOTAL ? 0 : 1))
