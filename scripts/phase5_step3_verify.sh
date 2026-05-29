#!/usr/bin/env bash
# =============================================================================
# scripts/phase5_step3_verify.sh
#
# Phase 5 Step 3 done-definition — pre-commit hook + .env housekeeping
# (R-P4-3, R-P4-5).
#
#   1. .pre-commit-config.yaml declares the fastapi-pydantic-freshness hook
#   2. The hook's entry script is executable + lives where the hook points
#   3. With fastapi container fresh, `check_..._freshness.sh --quiet` exits 0
#   4. Touching a watched file flips the script's exit code to 1
#      (proves the hook would block a commit when the container is stale)
#   5. After restoring the file's mtime, the script returns to exit 0
#   6. .env has no leftover orphan vars from Phase 4 Step 7
#   7. The pre-existing system-prompt-version-bump hook is still wired
#      (regression — the new hook didn't displace the old one)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
CFG="$REPO/.pre-commit-config.yaml"
SCRIPT="$REPO/scripts/check_fastapi_pydantic_freshness.sh"

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
PHASE 5 STEP 3 — PRE-COMMIT + .ENV HOUSEKEEPING VERIFICATION
============================================================
BANNER

# 1) Hook declared
if grep -q 'id: fastapi-pydantic-freshness' "$CFG"; then
    check "fastapi-pydantic-freshness hook declared in .pre-commit-config.yaml" ok
else
    check "hook declared" fail "id missing from $CFG"
fi

# 2) Entry script exists + executable
if [ -x "$SCRIPT" ]; then
    check "check_fastapi_pydantic_freshness.sh exists + is executable" ok
else
    check "entry script" fail "$SCRIPT not executable"
fi

# 3) Fresh state — should exit 0
if bash "$SCRIPT" --quiet; then
    fresh_exit=0
else
    fresh_exit=$?
fi
[ "$fresh_exit" = "0" ] \
    && check "Fresh container → freshness script exits 0" ok \
    || check "fresh exit" fail "got $fresh_exit (expected 0)"

# 4) Touch a watched file → should flip to exit 1.
PROBE="$REPO/src/fastapi/app/services/flow_jwt.py"
if [ ! -f "$PROBE" ]; then
    check "probe file present" fail "$PROBE missing"
else
    orig_mtime=$(stat -c '%Y' "$PROBE")
    touch "$PROBE"
    sleep 1
    bash "$SCRIPT" --quiet
    stale_exit=$?
    # restore mtime BEFORE asserting so we don't leave the tree dirty
    touch -d "@$orig_mtime" "$PROBE"
    [ "$stale_exit" = "1" ] \
        && check "Stale file → freshness script exits 1" ok \
        || check "stale exit" fail "got $stale_exit (expected 1)"
fi

# 5) After mtime restore, script back to exit 0
if bash "$SCRIPT" --quiet; then
    post_exit=0
else
    post_exit=$?
fi
[ "$post_exit" = "0" ] \
    && check "After mtime restore → script back to exit 0" ok \
    || check "post-restore" fail "got $post_exit (expected 0)"

# 6) .env orphan-var sweep
ORPHANS=(POSTGRES_MAX_CONNECTIONS QDRANT_HNSW_M QDRANT_HNSW_EF_CONSTRUCT
         QDRANT_HNSW_EF PROMETHEUS_RETENTION LANGFUSE_TRACING LANGFUSE_PROJECT)
remaining=0
for k in "${ORPHANS[@]}"; do
    if grep -qE "^${k}=" "$REPO/.env"; then
        remaining=$((remaining + 1))
    fi
done
[ "$remaining" = "0" ] \
    && check ".env has none of the 7 Phase 4 Step 7 orphan keys" ok \
    || check ".env housekeeping" fail "$remaining orphan key(s) still present"

# 7) Pre-existing prompt-version hook still wired
if grep -q 'id: system-prompt-version-bump' "$CFG"; then
    check "Pre-existing system-prompt-version-bump hook still configured" ok
else
    check "regression: prompt hook" fail "displaced from $CFG"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
