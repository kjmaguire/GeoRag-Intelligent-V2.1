#!/usr/bin/env bash
# Phase 16 Step 2 — roadmap doc.
set -uo pipefail
PASS=0; TOTAL=4
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/roadmap_phase16_onward.md"

check() {
    if [ "$2" = ok ]; then echo "  [PASS] $1"; PASS=$((PASS+1)); else echo "  [FAIL] $1 — $3"; fi
}

echo
echo "PHASE 16 STEP 2 — ROADMAP DOC"
echo "============================================================"

[ -s "$DOC" ] && check "Roadmap doc present" ok || check "doc" fail "missing"

paths=$(grep -cE '^### Path [A-C](\.[0-9]+)?' "$DOC" || true)
[ "${paths:-0}" -ge 3 ] 2>/dev/null \
    && check "Roadmap names ≥3 candidate paths (got $paths)" ok \
    || check "paths" fail "$paths"

efforts=$(grep -c 'Effort:' "$DOC" || true)
[ "${efforts:-0}" -ge 3 ] 2>/dev/null \
    && check "≥3 effort estimates given (got $efforts)" ok \
    || check "efforts" fail "$efforts"

if grep -q 'Recommended Phase 16' "$DOC" \
    && grep -q 'Phase 17' "$DOC"; then
    check "Doc proposes a concrete Phase 16 + Phase 17 pairing" ok
else
    check "recommendation" fail "missing"
fi

echo
echo "Result: $PASS / $TOTAL checks passed"
exit $((PASS == TOTAL ? 0 : 1))
