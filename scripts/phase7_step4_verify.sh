#!/usr/bin/env bash
# =============================================================================
# scripts/phase7_step4_verify.sh
#
# Phase 7 Step 4 done-definition — rollup filename rationalisation
# (R-P6-4).
#
#   1. database/raw/current-rollup.sql exists + non-empty
#   2. database/raw/phase0-4-rollup.sql still exists (back-compat alias)
#   3. Both files have identical content (byte-for-byte modulo the
#      generated-at timestamp line)
#   4. Header banner reflects the auto-detected latest phase (e.g.
#      "Phase 0-7 Cumulative Rollup")
#   5. Rebuild is reproducible — re-running the builder produces the
#      same content (timestamp stripped)
#   6. Phase 4 Step 7 verifier still passes against the legacy path
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
PRIMARY="$REPO/database/raw/current-rollup.sql"
LEGACY="$REPO/database/raw/phase0-4-rollup.sql"

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
PHASE 7 STEP 4 — ROLLUP FILENAME RATIONALISATION VERIFICATION
============================================================
BANNER

# 1) Canonical file exists + non-empty
if [ -s "$PRIMARY" ]; then
    lines=$(wc -l < "$PRIMARY")
    check "current-rollup.sql exists + non-empty ($lines lines)" ok
else
    check "primary file" fail "missing or empty: $PRIMARY"
fi

# 2) Legacy alias exists
if [ -s "$LEGACY" ]; then
    check "phase0-4-rollup.sql (legacy alias) still present" ok
else
    check "legacy alias" fail "missing or empty: $LEGACY"
fi

# 3) Content parity (strip timestamp line for comparison)
md5_primary=$(grep -v '^-- Generated at:' "$PRIMARY" | md5sum | cut -d' ' -f1)
md5_legacy=$(grep -v '^-- Generated at:' "$LEGACY" | md5sum | cut -d' ' -f1)
[ "$md5_primary" = "$md5_legacy" ] \
    && check "current-rollup.sql + phase0-4-rollup.sql have identical content" ok \
    || check "content parity" fail "md5 differs: $md5_primary vs $md5_legacy"

# 4) Header banner reflects latest phase
latest=$(ls -d "$REPO/database/raw"/phase[0-9]* 2>/dev/null \
    | sed -E 's|.*/phase([0-9]+).*|\1|' | sort -n | tail -1)
if head -3 "$PRIMARY" | grep -qE "Phase 0-${latest:-[0-9]+} Cumulative Rollup"; then
    check "Banner reflects auto-detected latest phase (Phase 0-$latest)" ok
else
    check "banner phase" fail "banner does not include Phase 0-$latest"
fi

# 5) Rebuild reproducible
md5_before=$(grep -v '^-- Generated at:' "$PRIMARY" | md5sum | cut -d' ' -f1)
bash "$REPO/scripts/phase4_step7_build_rollup.sh" >/dev/null
md5_after=$(grep -v '^-- Generated at:' "$PRIMARY" | md5sum | cut -d' ' -f1)
[ "$md5_before" = "$md5_after" ] \
    && check "Rebuild is reproducible (md5 stable across regen)" ok \
    || check "rebuild reproducibility" fail "md5 drift"

# 6) Phase 4 Step 7 regression
p4s7=$(bash "$REPO/scripts/phase4_step7_verify.sh" 2>&1 | grep -E '^Result: ' | tail -1)
case "$p4s7" in
    'Result: 5 / 5 checks passed')
        check "Phase 4 Step 7 verifier still passes 5/5 against legacy alias" ok ;;
    *) check "phase4_step7 regression" fail "$p4s7" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
