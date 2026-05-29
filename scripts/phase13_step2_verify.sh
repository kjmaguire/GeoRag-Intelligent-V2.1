#!/usr/bin/env bash
# =============================================================================
# scripts/phase13_step2_verify.sh
#
# Phase 13 Step 2 — golden-query fixture spec doc.
#
#   1. docs/phase13_golden_fixture_spec.md exists + non-trivial
#   2. Doc references the canonical TEST_PROJECT_ID UUID
#   3. Doc lists all 10 PLS-* hole IDs
#   4. Doc captures schema dependencies (silver.projects + silver.collars)
#   5. Doc documents the CRS (EPSG:32613) + geom_4326 transformation
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase13_golden_fixture_spec.md"

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
PHASE 13 STEP 2 — GOLDEN FIXTURE SPEC DOC
============================================================
BANNER

# 1) Doc exists + non-trivial
if [ -s "$DOC" ]; then
    lines=$(wc -l < "$DOC")
    if [ "$lines" -ge 60 ]; then
        check "Spec doc present ($lines lines)" ok
    else
        check "doc length" fail "$lines lines (≥60 required)"
    fi
else
    check "doc exists" fail "missing"
fi

# 2) TEST_PROJECT_ID
if grep -q '019d74a1-fba8-7165-9ae6-a5bf93eef97d' "$DOC"; then
    check "Doc references canonical TEST_PROJECT_ID" ok
else
    check "project id" fail "missing"
fi

# 3) All 10 hole IDs
missing=()
for h in PLS-20-01 PLS-20-02 PLS-20-03 PLS-20-04 PLS-21-05 PLS-21-06 PLS-21-07 PLS-22-08 PLS-22-09 PLS-22-10; do
    grep -q "$h" "$DOC" || missing+=("$h")
done
if [ "${#missing[@]}" -eq 0 ]; then
    check "Doc lists all 10 PLS-* hole IDs" ok
else
    check "hole ids" fail "${missing[*]}"
fi

# 4) Schema dependencies
if grep -q 'silver.projects' "$DOC" \
    && grep -q 'silver.collars' "$DOC"; then
    check "Doc captures both schema dependencies" ok
else
    check "schema deps" fail "missing"
fi

# 5) CRS + geom_4326
if grep -q 'EPSG:32613' "$DOC" \
    && grep -q 'ST_Transform' "$DOC" \
    && grep -q 'geom_4326' "$DOC"; then
    check "Doc documents CRS + geom_4326 transformation" ok
else
    check "crs docs" fail "missing"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
