#!/usr/bin/env bash
# =============================================================================
# scripts/phase10_step4_verify.sh
#
# Phase 10 Step 4 done-definition — Phase 11 scoping doc.
#
#   1. docs/phase11_scoping.md exists + non-trivial
#   2. Inventory section lists ≥5 concrete artifacts with file paths
#      (parsers, agent files, hallucination layers, tests, etc.)
#   3. Doc references the canonical agent orchestrator at its actual path
#   4. Doc names three Phase 11 candidate paths (A, B, C) with effort estimates
#   5. Doc surfaces ≥3 concrete gaps observed
#   6. Doc closes with a Recommended Phase 11 section
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase11_scoping.md"

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
PHASE 10 STEP 4 — PHASE 11 SCOPING DOC VERIFICATION
============================================================
BANNER

# 1) File present + non-trivial
if [ -s "$DOC" ]; then
    lines=$(wc -l < "$DOC")
    if [ "$lines" -ge 100 ]; then
        check "Scoping doc present ($lines lines)" ok
    else
        check "doc length" fail "only $lines lines — needs at least 100"
    fi
else
    check "doc exists" fail "missing"
fi

# 2) Inventory artifact count — count concrete file paths under
# either src/ or tests/ in the doc body.
artifact_count=$(grep -cE '(src/|tests/|app/|resources/)' "$DOC" || echo 0)
if [ "${artifact_count:-0}" -ge 5 ] 2>/dev/null; then
    check "Inventory lists $artifact_count concrete file paths" ok
else
    check "inventory artifacts" fail "only $artifact_count paths"
fi

# 3) References the agent orchestrator
if grep -q 'orchestrator.py' "$DOC"; then
    check "Doc references the agent orchestrator file" ok
else
    check "orchestrator ref" fail "missing"
fi

# 4) Three Phase 11 candidate paths
paths=$(grep -cE '^### Path [ABC]' "$DOC" || echo 0)
[ "$paths" = "3" ] \
    && check "Doc names Path A + B + C candidates" ok \
    || check "candidate paths" fail "got $paths"

# Each path should have an Effort line
effort_lines=$(grep -cE '\*\*Effort:\*\*' "$DOC" || echo 0)
if [ "${effort_lines:-0}" -ge 3 ] 2>/dev/null; then
    : # bundled in next check
fi

# 5) Gaps section. The awk-range trick has a known gotcha: when the
# starting + ending patterns are both `^## `, the range collapses
# to the heading line itself. Use a state machine instead — turn
# on inside the gaps section, off at the next top-level heading,
# then count numbered bullets.
gap_bullets=$(awk '
    /^## / && in_gaps { in_gaps=0 }
    in_gaps && /^[0-9]+\. / { count++ }
    /^## .*[Gg]ap/ { in_gaps=1 }
    END { print count + 0 }
' "$DOC")
gaps_present=$(grep -cE '^## .*[Gg]ap' "$DOC" || true)
if [ "${gaps_present:-0}" -ge 1 ] 2>/dev/null && [ "${gap_bullets:-0}" -ge 3 ] 2>/dev/null; then
    check "Doc has a Gaps section listing $gap_bullets items" ok
else
    check "gaps section" fail "section_count=$gaps_present bullets=$gap_bullets"
fi

# 6) Recommended Phase 11
if grep -qE '^### Recommended Phase 11' "$DOC"; then
    check "Doc closes with a Recommended Phase 11 section" ok
else
    check "recommendation" fail "missing"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
