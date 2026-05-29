#!/usr/bin/env bash
# =============================================================================
# scripts/phase4_step3_verify.sh
#
# Phase 4 Step 3 done-definition — fastapi/Pydantic freshness CI check.
#
#   1. check_fastapi_pydantic_freshness.sh exists + executable
#   2. Currently green (no stale files; we just restarted fastapi at Step 2)
#   3. After touching a watched file, script correctly reports STALE
#   4. After restarting fastapi, script returns green again
#   5. --quiet mode: exit code matches non-quiet, no stdout
#   6. Container-not-running → exit 2 (canonical "infrastructure error")
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
SCRIPT=/home/georag/projects/georag/scripts/check_fastapi_pydantic_freshness.sh

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
PHASE 4 STEP 3 — FASTAPI PYDANTIC FRESHNESS VERIFICATION
============================================================
BANNER

# 1) Script present + executable
if [ -x "$SCRIPT" ]; then
    check "check_fastapi_pydantic_freshness.sh present + executable" ok
else
    check "script" fail "missing or not executable"
fi

# 2) Baseline green — restart fastapi first so file syncs from the
# dev environment don't trip the check at run-start.
docker compose -f /home/georag/projects/georag/docker-compose.yml restart fastapi >/dev/null 2>&1
for i in $(seq 1 30); do
    s=$(docker inspect --format='{{.State.Status}}' georag-fastapi 2>/dev/null)
    if [ "$s" = "running" ]; then break; fi
    sleep 2
done
if bash "$SCRIPT" --quiet; then
    check "Baseline green after explicit fastapi restart" ok
else
    check "baseline" fail "stale immediately after restart — clock drift?"
fi

# 3) Touch a watched file → STALE. Wait long enough after the baseline
# restart that the file mtime is definitively newer than the container's
# StartedAt. Two-second resolution is plenty for second-granularity stat.
sleep 3
touch /home/georag/projects/georag/src/fastapi/app/hatchet_workflows/external_notification.py
sleep 1
if bash "$SCRIPT" --quiet; then
    check "After touching file, script reports STALE" fail "stayed green"
else
    rc=$?
    if [ "$rc" = "1" ]; then
        check "After touching watched file, script reports STALE (exit 1)" ok
    else
        check "stale detection" fail "got exit code $rc (expected 1)"
    fi
fi

# 4) Restart fastapi → green again
docker compose -f /home/georag/projects/georag/docker-compose.yml restart fastapi >/dev/null 2>&1
# Wait briefly for the container to come back up.
for i in $(seq 1 30); do
    s=$(docker inspect --format='{{.State.Status}}' georag-fastapi 2>/dev/null)
    if [ "$s" = "running" ]; then break; fi
    sleep 2
done
if bash "$SCRIPT" --quiet; then
    check "After fastapi restart, script reports green again" ok
else
    check "post-restart state" fail "still stale after restart"
fi

# 5) --quiet writes nothing to stdout
out=$(bash "$SCRIPT" --quiet 2>&1)
[ -z "$out" ] \
    && check "--quiet produces no stdout" ok \
    || check "quiet mode" fail "output: $out"

# 6) Container-not-running → exit 2.  We don't actually stop fastapi —
#    instead point the script at a non-existent container name.
FASTAPI_CONTAINER=nonexistent_container_phase4_step3 bash "$SCRIPT" --quiet
rc=$?
[ "$rc" = "2" ] \
    && check "Container-not-running → exit 2 (infrastructure error)" ok \
    || check "container-down exit code" fail "got $rc (expected 2)"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
