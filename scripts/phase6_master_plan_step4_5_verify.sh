#!/usr/bin/env bash
# Master-plan §6 sub-steps 6.4 + 6.5 verifier (doc-phase 76).
#
# §6.4: Public/Private Boundary Agent skeleton (app.agents.phase6)
# §6.5: silver.saved_map_views table + RLS policy
#
# Asserts:
#   1. app/agents/phase6/__init__.py + public_private_boundary.py exist
#   2. public_private_boundary import succeeds inside georag-fastapi
#   3. silver.saved_map_views exists with expected columns
#   4. RLS policy saved_map_views_workspace_isolation present
#   5. Laravel migrations row recorded
#   6. §5.3-5 and §3 manifest cascade still green

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

source "$SCRIPT_DIR/_verifier_manifest.sh"

PG_CONTAINER="${PG_CONTAINER:-georag-postgresql}"
FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
PSQL="docker exec $PG_CONTAINER psql -U georag -d georag -tAX"

FAIL=0
note() { echo "$1"; }

# ----------------------------------------------------------------------
# Check 1 — phase6 agent module files exist
# ----------------------------------------------------------------------
PHASE6_DIR="$REPO_ROOT/src/fastapi/app/agents/phase6"
if [ -f "$PHASE6_DIR/__init__.py" ] && [ -f "$PHASE6_DIR/public_private_boundary.py" ]; then
    note "[check1] PASS — app/agents/phase6/{__init__,public_private_boundary}.py present"
else
    note "[check1] FAIL — phase6 agent module files missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — import smoke-test inside fastapi container
# ----------------------------------------------------------------------
if docker exec "$FASTAPI_CONTAINER" python -c \
    "from app.agents.phase6 import public_private_boundary; assert callable(public_private_boundary)" \
    >/dev/null 2>&1; then
    note "[check2] PASS — public_private_boundary imports + is callable"
else
    note "[check2] FAIL — public_private_boundary import failed in $FASTAPI_CONTAINER"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — silver.saved_map_views table + columns
# ----------------------------------------------------------------------
exists=$($PSQL -c "SELECT 1 FROM pg_tables WHERE schemaname='silver' AND tablename='saved_map_views';")
if [ "$exists" = "1" ]; then
    note "[check3a] PASS — silver.saved_map_views exists"
else
    note "[check3a] FAIL — silver.saved_map_views missing"
    FAIL=$((FAIL + 1))
fi

col_count=$($PSQL -c "SELECT count(*) FROM information_schema.columns WHERE table_schema='silver' AND table_name='saved_map_views' AND column_name IN ('view_id','workspace_id','project_id','user_id','name','view_state','aoi_geom','is_shared');")
if [ "$col_count" = "8" ]; then
    note "[check3b] PASS — all 8 expected columns present"
else
    note "[check3b] FAIL — expected 8 columns, found $col_count"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4 — RLS policy
# ----------------------------------------------------------------------
policy_count=$($PSQL -c "SELECT count(*) FROM pg_policies WHERE schemaname='silver' AND tablename='saved_map_views' AND policyname='saved_map_views_workspace_isolation';")
if [ "$policy_count" = "1" ]; then
    note "[check4] PASS — RLS policy saved_map_views_workspace_isolation present"
else
    note "[check4] FAIL — RLS policy missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 5 — Laravel migration row
# ----------------------------------------------------------------------
recorded=$($PSQL -c "SELECT count(*) FROM migrations WHERE migration='2026_05_13_090000_create_silver_saved_map_views';")
if [ "$recorded" = "1" ]; then
    note "[check5] PASS — §6.5 migration recorded in Laravel migrations table"
else
    note "[check5] FAIL — §6.5 migration row missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 6 — §3 and §5 cascade still green
# ----------------------------------------------------------------------
for step in 8a 8b 8c 8d 8e 8f 8g "5.3-5"; do
    if check_verifier_recent "step${step}"; then
        note "[step${step}] PASS — manifest recent (no regression)"
    else
        note "[step${step}] WARN — manifest entry stale; re-run that verifier to refresh."
    fi
done

echo ""
echo "=== §6 Step 4-5 verifier summary ==="
echo "  $((6 - FAIL))/6 checks (excluding manifest warnings)"

if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step6.4-5"
fi

exit $FAIL
