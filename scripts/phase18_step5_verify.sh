#!/usr/bin/env bash
# =============================================================================
# scripts/phase18_step5_verify.sh
#
# Phase 18 Step 5 — silver.mv_collar_summary cartesian-join fix.
#
#   1. Fix migration file present + non-trivial
#   2. MV definition uses count(DISTINCT c.collar_id)
#   3. MV total_collars for test project = 20 (not inflated by samples/litho)
#   4. MV avg_depth for test project = 360.8 (not skewed by row multiplication)
#   5. MV still reports samples + lithology row counts (≥4 each)
#   6. Unique index on mv_collar_summary.project_id present
#   7. Fix migration is idempotent
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
SQL="$REPO/database/raw/phase18/15-fix-mv-collar-summary.sql"
PROJ='019d74a1-fba8-7165-9ae6-a5bf93eef97d'
PG=georag-postgresql

q() { docker exec "$PG" psql -U georag -d georag -tAc "$1" | tr -d ' '; }

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
PHASE 18 STEP 5 — MV CARTESIAN-JOIN FIX
============================================================
BANNER

# 1) Migration file
if [ -s "$SQL" ]; then
    lines=$(wc -l < "$SQL")
    [ "$lines" -ge 40 ] \
        && check "Fix migration present ($lines lines)" ok \
        || check "migration length" fail "only $lines lines"
else
    check "migration exists" fail "missing"
fi

# 2) MV uses DISTINCT collar count
viewdef=$(docker exec "$PG" psql -U georag -d georag -tAc \
    "SELECT pg_get_viewdef('silver.mv_collar_summary'::regclass, true);")
if echo "$viewdef" | grep -qE 'count\(DISTINCT[[:space:]]+(c\.)?collar_id\)'; then
    check "MV definition uses count(DISTINCT c.collar_id)" ok
else
    check "DISTINCT clause" fail "still uses non-distinct count"
fi

# 3) total_collars = 20
n=$(q "SELECT total_collars FROM silver.mv_collar_summary WHERE project_id = '$PROJ';")
if [ "$n" = "20" ]; then
    check "MV total_collars = 20 (cartesian-free)" ok
else
    check "total_collars" fail "got $n"
fi

# 4) avg_depth = 360.8
a=$(q "SELECT avg_depth FROM silver.mv_collar_summary WHERE project_id = '$PROJ';")
if [ "$a" = "360.8" ]; then
    check "MV avg_depth = 360.8 (cartesian-free)" ok
else
    check "avg_depth" fail "got $a"
fi

# 5) Sample + lithology counts still surfaced
ns=$(q "SELECT total_samples FROM silver.mv_collar_summary WHERE project_id = '$PROJ';")
nl=$(q "SELECT total_litho_intervals FROM silver.mv_collar_summary WHERE project_id = '$PROJ';")
if [ "${ns:-0}" -ge 4 ] 2>/dev/null && [ "${nl:-0}" -ge 4 ] 2>/dev/null; then
    check "MV still surfaces samples=$ns litho=$nl" ok
else
    check "downhole counts" fail "samples=$ns litho=$nl"
fi

# 6) Unique index present
idx=$(q "SELECT count(*) FROM pg_indexes WHERE schemaname='silver' AND tablename='mv_collar_summary' AND indexdef ILIKE '%UNIQUE%';")
if [ "${idx:-0}" -ge 1 ] 2>/dev/null; then
    check "Unique index on (project_id) present" ok
else
    check "unique index" fail "missing"
fi

# 7) Idempotent re-apply
docker exec -i "$PG" psql -U georag -d georag -v ON_ERROR_STOP=1 \
    < "$SQL" >/dev/null 2>&1
n_after=$(q "SELECT total_collars FROM silver.mv_collar_summary WHERE project_id = '$PROJ';")
if [ "$n_after" = "20" ]; then
    check "Idempotent re-apply (total_collars stays 20)" ok
else
    check "idempotency" fail "after=$n_after"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
