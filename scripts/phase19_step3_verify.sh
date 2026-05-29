#!/usr/bin/env bash
# =============================================================================
# scripts/phase19_step3_verify.sh
#
# Phase 19 Step 3 — baseline v4 doc.
#
#   1. Baseline v4 present + non-trivial
#   2. Doc records Phase 19 cold-run peak = 19
#   3. Doc identifies gq-011 + gq-012 as Phase 19 unlocks
#   4. Doc lists gq-013/018/025 carry-over reasons
#   5. Doc names new failure class A3 (graph property surface)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase19_golden_baseline_v4.md"

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
PHASE 19 STEP 3 — BASELINE V4
============================================================
BANNER

# 1) Present + non-trivial
if [ -s "$DOC" ]; then
    lines=$(wc -l < "$DOC")
    [ "$lines" -ge 80 ] \
        && check "Baseline v4 present ($lines lines)" ok \
        || check "doc length" fail "only $lines lines"
else
    check "doc exists" fail "missing"
fi

# 2) Peak 19 recorded
if grep -qE '\| \*\*19\*\* \| \*\*31\*\* \| \*\*19\*\*' "$DOC"; then
    check "Doc records Phase 19 cold-run peak = 19" ok
else
    check "peak record" fail "table row missing"
fi

# 3) gq-011 + gq-012 unlocks named
if grep -q 'gq-011' "$DOC" && grep -q 'gq-012' "$DOC" && grep -qi 'unlock' "$DOC"; then
    check "Doc identifies gq-011 + gq-012 as Phase 19 unlocks" ok
else
    check "unlocks" fail "missing references"
fi

# 4) Carry-over reasons for gq-013/018/025
for t in gq-013 gq-018 gq-025; do
    if ! grep -q "$t" "$DOC"; then
        check "carry $t" fail "not mentioned"
        continue 2
    fi
done
check "Doc lists gq-013 + gq-018 + gq-025 carry-overs" ok

# 5) Class A3 named
if grep -qE 'A3|graph property surface' "$DOC"; then
    check "Doc names failure class A3 (graph property surface)" ok
else
    check "A3 class" fail "not declared"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
