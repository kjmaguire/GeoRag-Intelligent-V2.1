#!/usr/bin/env bash
# =============================================================================
# scripts/phase18_step4_verify.sh
#
# Phase 18 Step 4 — baseline v3 doc + handoff.
#
#   1. Baseline v3 doc present + non-trivial
#   2. Doc records Phase 18 cold-run peak ≥ 16
#   3. Doc identifies gq-015 as the new unlock
#   4. Doc carries over gq-014 + gq-017 with reason (phrase-rendering)
#   5. Doc lists ≥2 deferred carry-overs for Phase 19+
#   6. Handoff doc present + non-trivial
#   7. Handoff lists Phase 18 deliverables table
#   8. Master sweep script present + executable
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=9
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase18_golden_baseline_v3.md"
HANDOFF="$REPO/docs/phase18_handoff.md"
SWEEP="$REPO/scripts/phase18_master_sweep.sh"

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
PHASE 18 STEP 4 — BASELINE V3 + HANDOFF
============================================================
BANNER

# 1) Doc present
if [ -s "$DOC" ]; then
    lines=$(wc -l < "$DOC")
    [ "$lines" -ge 60 ] \
        && check "Baseline v3 present ($lines lines)" ok \
        || check "doc length" fail "only $lines lines"
else
    check "doc exists" fail "missing"
fi

# 2) Peak ≥16 recorded
if grep -qE '\| \*\*18\*\* \| \*\*31\*\* \| \*\*16\*\*' "$DOC"; then
    check "Doc records Phase 18 cold-run peak = 16" ok
else
    check "peak record" fail "table row missing"
fi

# 3) gq-015 named as unlock
if grep -q 'gq-015' "$DOC"; then
    check "Doc identifies gq-015 as Phase 18 unlock" ok
else
    check "unlock named" fail "gq-015 not mentioned"
fi

# 4) gq-014 + gq-017 carry-over reasons
if grep -q 'gq-014' "$DOC" && grep -q 'gq-017' "$DOC" && grep -qi 'phrase' "$DOC"; then
    check "Doc carries gq-014 + gq-017 with phrase-rendering reason" ok
else
    check "carry-overs" fail "missing 014/017/phrase rationale"
fi

# 5) ≥2 deferred carry-overs to Phase 19+
carry=$(grep -cE 'R-P14-3\.[467]|R-P11-baseline|R-P14-3\.6' "$DOC")
[ "${carry:-0}" -ge 2 ] 2>/dev/null \
    && check "Doc lists ≥2 deferred carry-overs (got $carry refs)" ok \
    || check "carry refs" fail "only $carry"

# 6) Handoff present
if [ -s "$HANDOFF" ]; then
    hlines=$(wc -l < "$HANDOFF")
    [ "$hlines" -ge 40 ] \
        && check "Phase 18 handoff present ($hlines lines)" ok \
        || check "handoff length" fail "only $hlines lines"
else
    check "handoff exists" fail "missing"
fi

# 7) Handoff has deliverables table
if grep -qE '^\| Step' "$HANDOFF" && grep -q 'Verifier' "$HANDOFF"; then
    check "Handoff lists deliverables table" ok
else
    check "deliverables table" fail "table header missing"
fi

# 8) Master sweep script
if [ -s "$SWEEP" ] && [ -x "$SWEEP" ]; then
    check "Master sweep present + executable" ok
else
    check "master sweep" fail "missing or not executable"
fi

# 9) Baseline doc records the Step 5 MV cartesian-join fix
if grep -qi 'cartesian\|count(DISTINCT c.collar_id)\|cartesian-free' "$DOC"; then
    check "Baseline doc records Step 5 MV fix" ok
else
    check "Step 5 record" fail "MV cartesian fix not mentioned"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
