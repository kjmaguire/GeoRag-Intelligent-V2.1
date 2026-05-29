#!/usr/bin/env bash
# Phase 18 Step 1 — schema audit doc.
set -uo pipefail
PASS=0; TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase18_assay_litho_schema_audit.md"

check() {
    if [ "$2" = ok ]; then echo "  [PASS] $1"; PASS=$((PASS+1)); else echo "  [FAIL] $1 — $3"; fi
}

echo
echo "PHASE 18 STEP 1 — schema audit"
echo "============================================================"

[ -s "$DOC" ] && check "Audit doc present" ok || check "doc" fail "missing"

if grep -q 'silver.samples' "$DOC" && grep -q 'silver.lithology_logs' "$DOC"; then
    check "Doc covers both target tables" ok
else
    check "tables" fail "missing"
fi

if grep -q 'workspace_id' "$DOC" && grep -q 'a0000000-0000-0000-0000-000000000001' "$DOC"; then
    check "Doc captures the workspace_id requirement + default value" ok
else
    check "workspace_id" fail "missing"
fi

if grep -q 'commodity_assays' "$DOC" \
    && grep -qE 'U3O8_?ppm|Au_?ppb' "$DOC"; then
    check "Doc captures commodity_assays JSON shape + key examples" ok
else
    check "assays shape" fail "missing"
fi

if grep -q 'SST' "$DOC" && grep -q 'PGN' "$DOC" \
    && grep -q 'OVB' "$DOC"; then
    check "Doc enumerates lithology codes for PLS-20-01 seed" ok
else
    check "litho codes" fail "missing"
fi

echo
echo "Result: $PASS / $TOTAL checks passed"
exit $((PASS == TOTAL ? 0 : 1))
