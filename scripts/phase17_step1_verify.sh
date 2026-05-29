#!/usr/bin/env bash
# Phase 17 Step 1 — failure audit doc.
set -uo pipefail
PASS=0; TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase17_golden_failure_audit.md"

check() {
    if [ "$2" = ok ]; then echo "  [PASS] $1"; PASS=$((PASS+1)); else echo "  [FAIL] $1 — $3"; fi
}

echo
echo "PHASE 17 STEP 1 — GOLDEN FAILURE AUDIT"
echo "============================================================"

[ -s "$DOC" ] && check "Audit doc present" ok || check "doc" fail "missing"

classes=$(grep -cE '^### [A-F]\. ' "$DOC" || true)
[ "${classes:-0}" -ge 6 ] 2>/dev/null \
    && check "Doc enumerates ≥6 failure classes (A-F, got $classes)" ok \
    || check "classes" fail "$classes"

tests_listed=$(grep -cE '^\| gq-' "$DOC" || true)
[ "${tests_listed:-0}" -ge 25 ] 2>/dev/null \
    && check "Doc references ≥25 individual gq-* tests" ok \
    || check "tests" fail "$tests_listed"

if grep -q 'Phase 17 Step 2 scope' "$DOC" \
    && grep -q 'Phase 18+ scope' "$DOC"; then
    check "Doc separates Phase 17 unlocks from Phase 18+ deferred work" ok
else
    check "scope split" fail "missing"
fi

if grep -qE 'XLS-24-\*|360\.8|uranium' "$DOC"; then
    check "Doc names the three Phase 17 Step 2 unlocks" ok
else
    check "unlocks" fail "missing"
fi

echo
echo "Result: $PASS / $TOTAL checks passed"
exit $((PASS == TOTAL ? 0 : 1))
