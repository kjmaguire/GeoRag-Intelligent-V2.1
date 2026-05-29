#!/usr/bin/env bash
# =============================================================================
# scripts/phase18_step2_verify.sh
#
# Phase 18 Step 2 — assay fixture on PLS-22-08.
#
#   1. Migration file present + non-trivial
#   2. silver.projects test project workspace_id linked to default workspace
#   3. ≥4 samples present on PLS-22-08 under the test project
#   4. peak U3O8_ppm across those samples = 52000
#   5. ≥2 samples carry an Au_ppb key
#   6. mv_collar_summary.total_samples picks up the new rows
#   7. Migration is idempotent (re-run leaves rowcount unchanged)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
SQL="$REPO/database/raw/phase18/10-assay-litho-fixture.sql"
PROJ='019d74a1-fba8-7165-9ae6-a5bf93eef97d'
WS='a0000000-0000-0000-0000-000000000001'
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
PHASE 18 STEP 2 — ASSAY FIXTURE (PLS-22-08 U3O8 + Au)
============================================================
BANNER

# 1) Migration file present + non-trivial
if [ -s "$SQL" ]; then
    lines=$(wc -l < "$SQL")
    [ "$lines" -ge 80 ] \
        && check "Migration present ($lines lines)" ok \
        || check "migration length" fail "only $lines lines"
else
    check "migration exists" fail "missing"
fi

# 2) Project workspace_id linked
ws=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT workspace_id FROM silver.projects WHERE project_id = '$PROJ';" | tr -d ' ')
if [ "$ws" = "$WS" ]; then
    check "Test project linked to default workspace" ok
else
    check "workspace link" fail "workspace_id=$ws"
fi

# 3) ≥4 samples on PLS-22-08
n_samples=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT count(*) FROM silver.samples s
      JOIN silver.collars c ON c.collar_id = s.collar_id
     WHERE c.project_id = '$PROJ' AND c.hole_id = 'PLS-22-08';" | tr -d ' ')
if [ "${n_samples:-0}" -ge 4 ] 2>/dev/null; then
    check "≥4 samples on PLS-22-08 (got $n_samples)" ok
else
    check "sample count" fail "got $n_samples < 4"
fi

# 4) Peak U3O8_ppm = 52000
peak=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT max((s.commodity_assays->>'U3O8_ppm')::numeric)
      FROM silver.samples s
      JOIN silver.collars c ON c.collar_id = s.collar_id
     WHERE c.project_id = '$PROJ' AND c.hole_id = 'PLS-22-08';" | tr -d ' ')
if [ "$peak" = "52000" ]; then
    check "Peak U3O8_ppm = 52000" ok
else
    check "U3O8 peak" fail "got peak=$peak"
fi

# 5) ≥2 samples carry Au_ppb
n_au=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT count(*) FROM silver.samples s
      JOIN silver.collars c ON c.collar_id = s.collar_id
     WHERE c.project_id = '$PROJ' AND c.hole_id = 'PLS-22-08'
       AND s.commodity_assays ? 'Au_ppb';" | tr -d ' ')
if [ "${n_au:-0}" -ge 2 ] 2>/dev/null; then
    check "≥2 samples carry Au_ppb (got $n_au)" ok
else
    check "Au_ppb count" fail "got $n_au < 2"
fi

# 6) MV picks up samples
mv_samples=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT total_samples FROM silver.mv_collar_summary
     WHERE project_id = '$PROJ';" | tr -d ' ')
if [ "${mv_samples:-0}" -ge 4 ] 2>/dev/null; then
    check "mv_collar_summary.total_samples reflects new rows (got $mv_samples)" ok
else
    check "mv samples" fail "got total_samples=$mv_samples"
fi

# 7) Idempotent re-apply — rowcount unchanged
docker exec -i "$PG" psql -U georag -d georag -v ON_ERROR_STOP=1 \
    < "$SQL" >/dev/null 2>&1 || true
n_after=$(docker exec "$PG" psql -U georag -d georag -tAc "
    SELECT count(*) FROM silver.samples s
      JOIN silver.collars c ON c.collar_id = s.collar_id
     WHERE c.project_id = '$PROJ' AND c.hole_id = 'PLS-22-08';" | tr -d ' ')
if [ "$n_after" = "$n_samples" ]; then
    check "Idempotent re-apply (count stays $n_after)" ok
else
    check "idempotency" fail "before=$n_samples after=$n_after"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
