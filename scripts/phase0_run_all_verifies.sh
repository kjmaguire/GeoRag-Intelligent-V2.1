#!/usr/bin/env bash
# Run every per-step Phase 0 verifier in sequence and emit a one-line summary.
set -uo pipefail
cd "$(dirname "$0")/.."

echo
echo "============================================================"
echo "PHASE 0 — ALL STEP VERIFIERS"
echo "============================================================"
TOTAL_PASS=0
TOTAL_TOTAL=0
FAILED_STEPS=()

for s in 1 2 3 4 5 6; do
    script="scripts/phase0_step${s}_verify.sh"
    if [ ! -x "$script" ]; then
        echo "  step ${s}: SKIPPED (no verifier at $script)"
        continue
    fi
    out=$(bash "$script" 2>&1)
    last=$(echo "$out" | grep -E '^Result: ' | tail -1)
    echo "  step ${s}: ${last:-FAILED (no Result line)}"
    if [[ "$last" =~ Result:\ ([0-9]+)\ /\ ([0-9]+) ]]; then
        TOTAL_PASS=$((TOTAL_PASS + ${BASH_REMATCH[1]}))
        TOTAL_TOTAL=$((TOTAL_TOTAL + ${BASH_REMATCH[2]}))
        if [ "${BASH_REMATCH[1]}" != "${BASH_REMATCH[2]}" ]; then
            FAILED_STEPS+=("step ${s}: ${BASH_REMATCH[1]}/${BASH_REMATCH[2]}")
        fi
    else
        FAILED_STEPS+=("step ${s}: parse error")
    fi
done

echo
echo "------------------------------------------------------------"
echo "Aggregate: ${TOTAL_PASS} / ${TOTAL_TOTAL} checks passed across all steps"
if [ ${#FAILED_STEPS[@]} -gt 0 ]; then
    echo "Steps with failures:"
    for f in "${FAILED_STEPS[@]}"; do echo "  - $f"; done
fi
echo "------------------------------------------------------------"

[ ${#FAILED_STEPS[@]} -eq 0 ]
