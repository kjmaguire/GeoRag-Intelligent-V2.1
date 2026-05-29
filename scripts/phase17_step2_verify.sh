#!/usr/bin/env bash
# Phase 17 Step 2 — Milestone-2 fixture extension.
set -uo pipefail
PASS=0; TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
SQL="$REPO/database/raw/phase17/10-golden-fixture-extensions.sql"
PROJ='019d74a1-fba8-7165-9ae6-a5bf93eef97d'

check() {
    if [ "$2" = ok ]; then echo "  [PASS] $1"; PASS=$((PASS+1)); else echo "  [FAIL] $1 — $3"; fi
}

q() { docker exec georag-postgresql psql -U georag -d georag -tAc "$1" 2>/dev/null | tr -d ' '; }

echo
echo "PHASE 17 STEP 2 — fixture extensions"
echo "============================================================"

# 1) Migration file present
[ -s "$SQL" ] && check "Migration SQL present" ok || check "sql" fail "missing"

# 2) Total collars = 20
n_total=$(q "SELECT count(*) FROM silver.collars WHERE project_id = '$PROJ';")
[ "$n_total" = "20" ] && check "20 collars under test project" ok || check "count" fail "got $n_total"

# 3) 10 XLS-24-* collars
n_xls=$(q "SELECT count(*) FROM silver.collars WHERE project_id = '$PROJ' AND hole_id LIKE 'XLS-24-%';")
[ "$n_xls" = "10" ] && check "10 XLS-24-* collars seeded" ok || check "xls count" fail "got $n_xls"

# 4) Project commodity = uranium
commodity=$(q "SELECT commodity FROM silver.projects WHERE project_id = '$PROJ';")
[ "$commodity" = "uranium" ] && check "Project commodity = 'uranium'" ok || check "commodity" fail "got '$commodity'"

# 5) Region trimmed to start with Athabasca
region=$(docker exec georag-postgresql psql -U georag -d georag -tAc "SELECT region FROM silver.projects WHERE project_id = '$PROJ';")
case "$region" in
    *Athabasca*) check "Project region contains 'Athabasca' (got '$(echo "$region" | xargs)')" ok ;;
    *) check "region" fail "got '$region'" ;;
esac

# 6) MV avg_depth = 360.8
avg_d=$(q "SELECT round(avg_depth::numeric, 1) FROM silver.mv_collar_summary WHERE project_id = '$PROJ';")
[ "$avg_d" = "360.8" ] && check "MV avg_depth = 360.8" ok || check "mv avg" fail "got $avg_d"

# 7) MV total_collars = 20
mv_total=$(q "SELECT total_collars FROM silver.mv_collar_summary WHERE project_id = '$PROJ';")
[ "$mv_total" = "20" ] && check "MV total_collars = 20" ok || check "mv total" fail "got $mv_total"

echo
echo "Result: $PASS / $TOTAL checks passed"
exit $((PASS == TOTAL ? 0 : 1))
