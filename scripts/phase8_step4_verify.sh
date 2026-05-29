#!/usr/bin/env bash
# =============================================================================
# scripts/phase8_step4_verify.sh
#
# Phase 8 Step 4 done-definition — Hatchet HA design doc (R-P3-6
# scoping, not implementation).
#
#   1. docs/phase8_hatchet_ha_design.md exists + non-trivial
#   2. Doc has all 6 expected key sections (Current posture,
#      Multi-instance, Worker-side adaptation, State-loss boundaries,
#      Operational ask, Recommendation)
#   3. Doc references the live hatchet-lite compose service name
#   4. Doc references the shared hatchet Postgres DB
#   5. Doc names at least one of the three Phase 9 paths (A/B/C)
#      with a concrete recommendation
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase8_hatchet_ha_design.md"

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
PHASE 8 STEP 4 — HATCHET HA DESIGN DOC VERIFICATION
============================================================
BANNER

# 1) File present + non-trivial
if [ -s "$DOC" ]; then
    lines=$(wc -l < "$DOC")
    if [ "$lines" -ge 80 ]; then
        check "Design doc present ($lines lines)" ok
    else
        check "doc length" fail "only $lines lines — needs at least 80"
    fi
else
    check "doc exists" fail "missing: $DOC"
fi

# 2) Six key sections present
missing=()
for section in \
    "Current posture" \
    "Multi-instance Hatchet engine" \
    "Worker-side adaptation" \
    "State-loss boundaries" \
    "Operational ask" \
    "Recommendation"; do
    grep -q "## .*$section" "$DOC" || missing+=("$section")
done
if [ "${#missing[@]}" -eq 0 ]; then
    check "All 6 key sections present" ok
else
    check "sections" fail "missing: ${missing[*]}"
fi

# 3) References hatchet-lite compose service
if grep -q 'hatchet-lite' "$DOC"; then
    check "Doc references the hatchet-lite compose service" ok
else
    check "compose ref" fail "no mention of hatchet-lite"
fi

# 4) References the shared Postgres DB
if grep -qE 'hatchet.*Postgres|Postgres.*hatchet|SERVER_MSGQUEUE_KIND=postgres' "$DOC"; then
    check "Doc references the shared hatchet Postgres backing store" ok
else
    check "pg ref" fail "no mention of Postgres backing store"
fi

# 5) Concrete Phase 9 paths + recommendation
if grep -qE '^### Path [ABC]' "$DOC" \
    && grep -qE '^### Recommendation' "$DOC"; then
    check "Doc names Path A/B/C options with a Recommendation section" ok
else
    check "phase9 paths" fail "missing Path A/B/C or Recommendation header"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
