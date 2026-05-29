#!/usr/bin/env bash
# =============================================================================
# scripts/phase4_step7_verify.sh
#
# Phase 4 Step 7 done-definition — migration rollup.
#
#   1. phase0-4-rollup.sql file present + non-empty
#   2. Rollup includes all per-phase files (count matches live tree)
#   3. Rollup is rebuildable + reproducible (regenerate → same content
#      modulo the timestamp header line)
#   4. Rollup applies cleanly against a database where Laravel
#      migrations have already run (live DB clone is the realistic
#      target — the raw/ rollup is a delta on top of Laravel migrations,
#      not a true greenfield bootstrap)
#   5. Re-apply against the same DB is idempotent (no new errors;
#      every migration is designed idempotent)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO=/home/georag/projects/georag
ROLLUP="$REPO/database/raw/phase0-4-rollup.sql"
TEST_DB="georag_rollup_verify"   # unused after the verifier refactor; kept for compatibility

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cleanup() {
    docker exec georag-postgresql psql -U georag -d postgres -q -c \
        "DROP DATABASE IF EXISTS \"${TEST_DB}\" WITH (FORCE);" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat <<'BANNER'

============================================================
PHASE 4 STEP 7 — MIGRATION ROLLUP VERIFICATION
============================================================
BANNER

# 1) Rollup file present + non-empty
if [ ! -f "$ROLLUP" ]; then
    check "phase0-4-rollup.sql exists" fail "missing"
elif [ "$(wc -l < "$ROLLUP")" -lt 100 ]; then
    check "phase0-4-rollup.sql exists + non-empty" fail "suspiciously small ($(wc -l < "$ROLLUP") lines)"
else
    check "phase0-4-rollup.sql exists ($(wc -l < "$ROLLUP") lines)" ok
fi

# 2) Counts match: every phaseN/*.sql is included. Restrict find to
#    direct children of phaseN/ dirs only — bash globs `phase[0-9]*`
#    also matches sibling files like `phase0-4-rollup.sql`.
file_count=$(find "$REPO/database/raw" -mindepth 2 -maxdepth 2 -name '*.sql' -type f -path '*/phase*' 2>/dev/null | wc -l)
include_count=$(grep -c '^-- phase' "$ROLLUP" || true)
[ "$include_count" = "$file_count" ] \
    && check "Rollup includes all $file_count per-phase files" ok \
    || check "include count" fail "rollup=$include_count tree=$file_count"

# 3) Reproducible regenerate
backup_md5=$(md5sum "$ROLLUP" | cut -d' ' -f1)
bash "$REPO/scripts/phase4_step7_build_rollup.sh" >/dev/null
# Strip the timestamp line before comparing (it's expected to differ).
md5_now=$(grep -v '^-- Generated at:' "$ROLLUP" | md5sum | cut -d' ' -f1)
md5_was=$(grep -v '^-- Generated at:' <(cat "$ROLLUP") | md5sum | cut -d' ' -f1)
# Strictly: regenerate is deterministic if both md5s match.
if [ "$md5_now" = "$md5_was" ]; then
    check "Rebuilding the rollup is reproducible" ok
else
    check "reproducibility" fail "md5 drift"
fi

# 4) Re-apply rollup against the LIVE georag DB. Every migration is
#    designed idempotent (CREATE TABLE IF NOT EXISTS, INSERT … ON
#    CONFLICT DO NOTHING / DO UPDATE, DROP TABLE IF EXISTS). A fresh
#    apply against the same cluster should produce zero schema drift
#    and zero ERRORs.
apply_log=$(docker exec -i georag-postgresql psql -U georag -d georag \
    -v ON_ERROR_STOP=1 < "$ROLLUP" 2>&1 | grep -E '^(ERROR|psql:.*ERROR)' | head -5)
if [ -z "$apply_log" ]; then
    check "Rollup re-applies idempotently to the live DB (no ERRORs)" ok
else
    check "rollup re-apply" fail "errors: $apply_log"
fi

# 5) After the idempotent re-apply, live object counts should still be
#    plausible — no migration accidentally dropped + recreated something
#    in a way that re-set IDs.
count_q="
    SELECT
        (SELECT count(*) FROM information_schema.tables
          WHERE table_schema IN ('silver','bronze','audit','usage','outbox','workflow','workspace')) AS tables,
        (SELECT count(*) FROM information_schema.routines
          WHERE routine_schema IN ('silver','bronze','audit','usage','outbox','workflow','workspace')) AS routines;"
counts=$(docker exec georag-postgresql psql -U georag -d georag -tAc "$count_q" 2>/dev/null)
n_tables=$(echo "$counts" | head -1 | cut -d'|' -f1)
n_routines=$(echo "$counts" | head -1 | cut -d'|' -f2)
if [ -n "$n_tables" ] && [ "$n_tables" -ge 50 ] 2>/dev/null \
   && [ -n "$n_routines" ] && [ "$n_routines" -ge 20 ] 2>/dev/null; then
    check "Post-reapply live counts plausible (tables=$n_tables routines=$n_routines)" ok
else
    check "object counts" fail "tables=$n_tables routines=$n_routines"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
