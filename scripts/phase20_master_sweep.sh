#!/usr/bin/env bash
# =============================================================================
# scripts/phase20_master_sweep.sh
#
# Run every verifier from Phase 0 → Phase 20.
# Two pre-existing non-greens are documented in Phase 18 handoff
# section 6 (phase4_step7 sweep-flake; phase9_step1 docker network).
# =============================================================================

set -uo pipefail

REPO="${REPO:-/home/georag/projects/georag}"
cd "$REPO"

# Inherit Phase 19's verifier list + add phase20_step1.
mapfile -t VERIFIERS < <(awk '/^    scripts\/phase[0-9]/{gsub(/^    /,""); gsub(/[[:space:]]*$/,""); print}' \
    "$REPO/scripts/phase19_master_sweep.sh")
VERIFIERS+=("scripts/phase20_step1_verify.sh")

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
echo "PHASE 0 → 20 MASTER SWEEP"
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
