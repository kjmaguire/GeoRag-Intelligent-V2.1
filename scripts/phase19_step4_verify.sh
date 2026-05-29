#!/usr/bin/env bash
# =============================================================================
# scripts/phase19_step4_verify.sh
#
# Phase 19 Step 4 — handoff + master sweep.
#
#   1. Handoff doc present + non-trivial
#   2. Handoff lists deliverables table
#   3. Handoff carries ≥4 deferred items into Phase 20+
#   4. Master sweep script present + executable
#   5. Master sweep includes all 4 Phase 19 verifiers
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
HANDOFF="$REPO/docs/phase19_handoff.md"
SWEEP="$REPO/scripts/phase19_master_sweep.sh"

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
PHASE 19 STEP 4 — HANDOFF + MASTER SWEEP
============================================================
BANNER

if [ -s "$HANDOFF" ]; then
    hlines=$(wc -l < "$HANDOFF")
    [ "$hlines" -ge 80 ] \
        && check "Handoff present ($hlines lines)" ok \
        || check "handoff length" fail "only $hlines lines"
else
    check "handoff exists" fail "missing"
fi

if grep -qE '^\| Step' "$HANDOFF" && grep -q 'Verifier' "$HANDOFF"; then
    check "Handoff lists deliverables table" ok
else
    check "deliverables table" fail "missing"
fi

carry=$(grep -cE '^\| \*\*R-P[0-9]+' "$HANDOFF" || true)
[ "${carry:-0}" -ge 4 ] 2>/dev/null \
    && check "Handoff carries ≥4 items into Phase 20+ (got $carry)" ok \
    || check "carry-overs" fail "only $carry"

if [ -s "$SWEEP" ] && [ -x "$SWEEP" ]; then
    check "Master sweep present + executable" ok
else
    check "master sweep" fail "missing or not executable"
fi

# All 4 phase19 verifiers referenced in sweep
miss=0
for v in phase19_step1_verify.sh phase19_step2_verify.sh \
         phase19_step3_verify.sh phase19_step4_verify.sh; do
    grep -q "$v" "$SWEEP" || miss=$((miss+1))
done
if [ "$miss" -eq 0 ]; then
    check "Master sweep includes all 4 Phase 19 verifiers" ok
else
    check "sweep coverage" fail "$miss Phase 19 verifiers missing"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
