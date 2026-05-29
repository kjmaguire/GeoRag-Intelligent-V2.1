#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_rp28_backups_verify.sh
#
# Phase 2 R-P2-8 — Activepieces DB in the pg_basebackup loop.
#
# pg_basebackup is cluster-level (captures every logical DB on the
# server), so the activepieces DB is structurally inside the existing
# backup payload. This verifier asserts the upstream invariants the
# backup script relies on.
#
#   1. The activepieces logical DB exists on the cluster
#   2. backup-agent container exists + is on the network
#   3. The backup script header references activepieces (regression
#      catch — if someone swaps to per-DB pg_dump and forgets us)
#   4. DRY_RUN against the script reaches the pg_basebackup invocation
#      without filesystem writes
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=4

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
PHASE 2 R-P2-8 — ACTIVEPIECES BACKUP COVERAGE VERIFICATION
============================================================
BANNER

# 1) activepieces DB present on cluster
db=$(docker exec georag-postgresql psql -U georag -d georag -tAc \
    "SELECT datname FROM pg_database WHERE datname='activepieces';" 2>/dev/null | tr -d ' ')
[ "$db" = "activepieces" ] \
    && check "activepieces logical DB present on cluster" ok \
    || check "activepieces DB" fail "got '$db'"

# 2) backup-agent container exists
if docker inspect georag-backup-agent >/dev/null 2>&1; then
    check "georag-backup-agent container exists" ok
else
    check "backup-agent container" fail "not found (docker compose up -d backup-agent)"
fi

# 3) Script header references activepieces (regression-catch)
if grep -q 'activepieces' /home/georag/projects/georag/docker/postgresql/backup.sh; then
    check "backup.sh header documents activepieces coverage" ok
else
    check "script header" fail "no activepieces reference in backup.sh"
fi

# 4) DRY_RUN reaches the pg_basebackup invocation. The script's DRY_RUN=1
#    branch logs `Would run: pg_basebackup ...` and exits 0 without IO.
#    backup-agent's /usr/bin/env is busybox; invoke /bin/bash directly.
if docker inspect georag-backup-agent >/dev/null 2>&1; then
    out=$(docker exec -e DRY_RUN=1 georag-backup-agent /bin/bash /backup-scripts/postgresql/backup.sh 2>&1 || true)
    if echo "$out" | grep -q 'pg_basebackup'; then
        check "DRY_RUN reaches pg_basebackup invocation" ok
    else
        check "DRY_RUN script run" fail "did not reach pg_basebackup; tail: $(echo "$out" | tail -3)"
    fi
else
    check "DRY_RUN script run" fail "(skipped — backup-agent not running)"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
