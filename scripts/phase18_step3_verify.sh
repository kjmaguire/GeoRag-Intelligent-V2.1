#!/usr/bin/env bash
# =============================================================================
# scripts/phase18_step3_verify.sh
#
# Phase 18 Step 3 — lithology fixture on PLS-20-01.
#
#   1. ≥4 lithology intervals on PLS-20-01 under the test project
#   2. SST code present (Athabasca Sandstone)
#   3. PGN code present (basement paragneiss)
#   4. OVB code present (overburden, top-of-hole)
#   5. Intervals are contiguous from 0m
#   6. mv_collar_summary.total_litho_intervals reflects new rows
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
PROJ='019d74a1-fba8-7165-9ae6-a5bf93eef97d'
PG=georag-postgresql

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
PHASE 18 STEP 3 — LITHOLOGY FIXTURE (PLS-20-01 SST + PGN)
============================================================
BANNER

# 1) ≥4 intervals
n_litho=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT count(*) FROM silver.lithology_logs ll
      JOIN silver.collars c ON c.collar_id = ll.collar_id
     WHERE c.project_id = '$PROJ' AND c.hole_id = 'PLS-20-01';" | tr -d ' ')
if [ "${n_litho:-0}" -ge 4 ] 2>/dev/null; then
    check "≥4 lithology intervals on PLS-20-01 (got $n_litho)" ok
else
    check "litho count" fail "got $n_litho < 4"
fi

# 2–4) Codes present
for code in SST PGN OVB; do
    n=$(docker exec "$PG" psql -U georag -d georag -tAc "
        SELECT count(*) FROM silver.lithology_logs ll
          JOIN silver.collars c ON c.collar_id = ll.collar_id
         WHERE c.project_id = '$PROJ' AND c.hole_id = 'PLS-20-01'
           AND ll.lithology_code = '$code';" | tr -d ' ')
    if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
        check "Lithology code $code present" ok
    else
        check "code $code" fail "missing"
    fi
done

# 5) Starts at 0m (overburden at top of hole)
top=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT min(ll.from_depth) FROM silver.lithology_logs ll
      JOIN silver.collars c ON c.collar_id = ll.collar_id
     WHERE c.project_id = '$PROJ' AND c.hole_id = 'PLS-20-01';" | tr -d ' ')
if [ "$top" = "0" ] || [ "$top" = "0.0" ]; then
    check "Lithology starts at 0m (top of hole)" ok
else
    check "top-of-hole" fail "min from_depth=$top"
fi

# 6) MV reflects intervals
mv_litho=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT total_litho_intervals FROM silver.mv_collar_summary
     WHERE project_id = '$PROJ';" | tr -d ' ')
if [ "${mv_litho:-0}" -ge 4 ] 2>/dev/null; then
    check "mv_collar_summary.total_litho_intervals reflects new rows (got $mv_litho)" ok
else
    check "mv litho" fail "got total_litho_intervals=$mv_litho"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
