#!/usr/bin/env bash
# =============================================================================
# scripts/phase13_step3_verify.sh
#
# Phase 13 Step 3 — silver.collars PLS-* fixture seeded (R-P11-baseline-1).
#
#   1. silver.projects has the TEST_PROJECT row
#   2. silver.collars has exactly 10 PLS-* rows under the test project
#   3. All 10 are Diamond hole_type
#   4. Status mix matches the test spec (9 Completed + 1 In Progress)
#   5. Depth bounds match spec (min=265, max=510)
#   6. Easting bounds match spec (min=493445, max≈498256.9)
#   7. geom + geom_4326 both populated (no NULLs)
#   8. Re-applying the migration is idempotent (no duplicate rows)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
REPO="${REPO:-/home/georag/projects/georag}"
SQL="$REPO/database/raw/phase13/10-golden-collars-fixture.sql"
PROJ='019d74a1-fba8-7165-9ae6-a5bf93eef97d'

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

q() {
    docker exec georag-postgresql psql -U georag -d georag -tAc "$1" 2>/dev/null | tr -d ' '
}

cat <<'BANNER'

============================================================
PHASE 13 STEP 3 — COLLAR FIXTURE VERIFICATION
============================================================
BANNER

# 1) Project row
n_proj=$(q "SELECT count(*) FROM silver.projects WHERE project_id = '$PROJ';")
[ "$n_proj" = "1" ] \
    && check "silver.projects has the TEST_PROJECT row" ok \
    || check "project row" fail "got $n_proj"

# 2) Ten PLS collars
n_pls=$(q "SELECT count(*) FROM silver.collars WHERE project_id = '$PROJ' AND hole_id LIKE 'PLS-%';")
[ "$n_pls" = "10" ] \
    && check "Exactly 10 PLS-* collars under test project" ok \
    || check "collar count" fail "got $n_pls"

# 3) All Diamond
n_diamond=$(q "SELECT count(*) FROM silver.collars WHERE project_id = '$PROJ' AND hole_id LIKE 'PLS-%' AND hole_type = 'Diamond';")
[ "$n_diamond" = "10" ] \
    && check "All 10 collars are Diamond hole_type" ok \
    || check "hole types" fail "$n_diamond / 10 Diamond"

# 4) Status mix
status_mix=$(q "SELECT (SELECT count(*) FROM silver.collars WHERE project_id='$PROJ' AND status='Completed' AND hole_id LIKE 'PLS-%') || '/' || (SELECT count(*) FROM silver.collars WHERE project_id='$PROJ' AND status='In Progress' AND hole_id LIKE 'PLS-%');")
[ "$status_mix" = "9/1" ] \
    && check "Status mix matches: 9 Completed + 1 In Progress" ok \
    || check "status mix" fail "got $status_mix"

# 5) Depth bounds
depth_bounds=$(q "SELECT min(total_depth) || '/' || max(total_depth) FROM silver.collars WHERE project_id = '$PROJ' AND hole_id LIKE 'PLS-%';")
[ "$depth_bounds" = "265/510" ] \
    && check "Depth bounds: min=265 / max=510" ok \
    || check "depth bounds" fail "got $depth_bounds"

# 6) Easting bounds
easting_bounds=$(q "SELECT round(min(easting)::numeric, 1) || '/' || round(max(easting)::numeric, 1) FROM silver.collars WHERE project_id = '$PROJ' AND hole_id LIKE 'PLS-%';")
[ "$easting_bounds" = "493445.0/498256.9" ] \
    && check "Easting bounds: min=493445.0 / max=498256.9" ok \
    || check "easting bounds" fail "got $easting_bounds"

# 7) Geometry populated
null_geom=$(q "SELECT count(*) FROM silver.collars WHERE project_id = '$PROJ' AND hole_id LIKE 'PLS-%' AND (geom IS NULL OR geom_4326 IS NULL);")
[ "$null_geom" = "0" ] \
    && check "All 10 rows have both geom + geom_4326 populated" ok \
    || check "geom nulls" fail "$null_geom rows with NULL geom"

# 8) Idempotent re-apply
docker exec -i georag-postgresql psql -U georag -d georag -v ON_ERROR_STOP=1 \
    < "$SQL" >/dev/null 2>&1
n_after=$(q "SELECT count(*) FROM silver.collars WHERE project_id = '$PROJ' AND hole_id LIKE 'PLS-%';")
[ "$n_after" = "10" ] \
    && check "Re-applying migration is idempotent ($n_after rows)" ok \
    || check "idempotency" fail "row count drifted to $n_after"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
