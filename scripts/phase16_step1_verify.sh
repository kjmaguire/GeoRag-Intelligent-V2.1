#!/usr/bin/env bash
# Phase 16 Step 1 — retrospective doc.
set -uo pipefail
PASS=0; TOTAL=4
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/retrospective_0_15.md"

check() {
    if [ "$2" = ok ]; then echo "  [PASS] $1"; PASS=$((PASS+1)); else echo "  [FAIL] $1 — $3"; fi
}

echo
echo "PHASE 16 STEP 1 — RETROSPECTIVE DOC"
echo "============================================================"

[ -s "$DOC" ] && check "Retrospective doc present" ok || check "doc" fail "missing"

phase_rows=$(grep -cE '^\| (1[0-5]?|[0-9]) \|' "$DOC" || true)
[ "${phase_rows:-0}" -ge 16 ] 2>/dev/null \
    && check "Retrospective covers ≥16 phase rows (got $phase_rows)" ok \
    || check "phases" fail "$phase_rows"

if grep -q '403' "$DOC" && grep -q '63' "$DOC"; then
    check "Cumulative count 403/63 verifiers recorded" ok
else
    check "totals" fail "missing"
fi

if grep -q 'Activepieces → Kestra' "$DOC" \
    && grep -q 'R-P13-1' "$DOC"; then
    check "Notable mid-run shifts captured (Kestra pivot + R-P13-1)" ok
else
    check "shifts" fail "missing"
fi

echo
echo "Result: $PASS / $TOTAL checks passed"
exit $((PASS == TOTAL ? 0 : 1))
