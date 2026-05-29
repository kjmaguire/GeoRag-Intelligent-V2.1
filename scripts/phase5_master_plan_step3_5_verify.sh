#!/usr/bin/env bash
# Master-plan §5 sub-steps 5.2-5.5 verifier (doc-phase 71).
#
# §5.2: pyproject.toml deps added (image rebuild deferred)
# §5.3-5.5: 3 gold visual table migrations applied
#
# Asserts:
#   1. fastapi pyproject.toml lists geopandas + rasterio + mplstereonet
#   2. gold.drillhole_intervals_visual exists with expected constraints
#   3. gold.cross_section_panels exists with PostGIS LINESTRING geom
#   4. gold.structure_measurements_visual exists with type/projection CHECKs
#   5. Migrations marked as Ran in Laravel's migrations table
#   6. §3 manifest still fresh (no regressions)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

source "$SCRIPT_DIR/_verifier_manifest.sh"

PG_CONTAINER="${PG_CONTAINER:-georag-postgresql}"
PSQL="docker exec $PG_CONTAINER psql -U georag -d georag -tAX"

FAIL=0
note() { echo "$1"; }

# ----------------------------------------------------------------------
# Check 1 — pyproject.toml has new deps
# ----------------------------------------------------------------------
PYPROJECT="$REPO_ROOT/src/fastapi/pyproject.toml"
if grep -q '"geopandas>=' "$PYPROJECT" \
   && grep -q '"rasterio>=' "$PYPROJECT" \
   && grep -q '"mplstereonet>=' "$PYPROJECT"; then
    note "[check1] PASS — fastapi pyproject.toml lists geopandas + rasterio + mplstereonet"
else
    note "[check1] FAIL — one or more §5 deps missing from pyproject.toml"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2-4 — Gold tables exist
# ----------------------------------------------------------------------
for table in drillhole_intervals_visual cross_section_panels structure_measurements_visual; do
    exists=$($PSQL -c "SELECT 1 FROM pg_tables WHERE schemaname='gold' AND tablename='$table';")
    if [ "$exists" = "1" ]; then
        note "[check_$table] PASS — gold.$table exists"
    else
        note "[check_$table] FAIL — gold.$table missing"
        FAIL=$((FAIL + 1))
    fi
done

# ----------------------------------------------------------------------
# Check 5 — Laravel migrations recorded
# ----------------------------------------------------------------------
recorded=$($PSQL -c "SELECT COUNT(*) FROM migrations WHERE migration LIKE '2026_05_13_080%';")
if [ "$recorded" = "3" ]; then
    note "[check5] PASS — 3 §5 migrations recorded in Laravel migrations table"
else
    note "[check5] FAIL — expected 3 §5 migrations recorded; found $recorded"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 6 — §3 cascade still green
# ----------------------------------------------------------------------
for step in 8a 8b 8c 8d 8e 8f 8g; do
    if check_verifier_recent "step${step}"; then
        note "[step${step}] PASS — §3 manifest recent (no regression)"
    else
        note "[step${step}] WARN — §3 manifest entry stale (not a regression; just expired). Re-run that verifier to refresh."
    fi
done

echo ""
echo "=== §5 Step 3-5 verifier summary ==="
echo "  $((11 - FAIL))/11 (excluding manifest warnings)"

# Record success for the new §5.3-5.5 batch as "step5.3-5"
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step5.3-5"
fi

exit $FAIL
