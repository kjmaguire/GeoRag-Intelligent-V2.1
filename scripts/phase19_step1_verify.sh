#!/usr/bin/env bash
# =============================================================================
# scripts/phase19_step1_verify.sh
#
# Phase 19 Step 1 — silver.reports.authors seed.
#
#   1. Author-seed migration present + non-trivial
#   2. ≥1 PLS-Property report has authors containing 'Sarah Thompson'
#   3. Migration is idempotent (re-run leaves rowcount unchanged)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=3
REPO="${REPO:-/home/georag/projects/georag}"
SQL="$REPO/database/raw/phase19/10-author-seed.sql"
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
PHASE 19 STEP 1 — silver.reports.authors SEED
============================================================
BANNER

if [ -s "$SQL" ]; then
    lines=$(wc -l < "$SQL")
    [ "$lines" -ge 25 ] \
        && check "Author-seed migration present ($lines lines)" ok \
        || check "migration length" fail "only $lines lines"
else
    check "migration exists" fail "missing"
fi

n=$(q "SELECT count(*) FROM silver.reports
        WHERE project_name = 'Patterson Lake South Property'
          AND 'Sarah Thompson' = ANY (authors);")
if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
    check "≥1 PLS report carries Sarah Thompson as author (got $n)" ok
else
    check "author count" fail "got $n"
fi

# Idempotent re-apply
docker exec -i "$PG" psql -U georag -d georag -v ON_ERROR_STOP=1 \
    < "$SQL" >/dev/null 2>&1
n_after=$(q "SELECT count(*) FROM silver.reports
              WHERE project_name = 'Patterson Lake South Property'
                AND 'Sarah Thompson' = ANY (authors);")
if [ "$n_after" = "$n" ]; then
    check "Idempotent re-apply (Sarah Thompson count stays $n)" ok
else
    check "idempotency" fail "before=$n after=$n_after"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
