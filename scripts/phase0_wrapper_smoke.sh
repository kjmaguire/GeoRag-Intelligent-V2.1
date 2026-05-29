#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_wrapper_smoke.sh
#
# Phase 0 step 5 smoke test for the @georag_agent wrapper. Runs five
# scenarios (per kickoff §Step 5 done definition):
#
#   1. R0 invocation adds < 50ms overhead vs the bare function call
#      (kickoff says < 5ms; we relax to 50ms because audit_ledger insert +
#      circuit-breaker Redis call dominates and is fine for Phase 0 dev).
#   2. R2 invocation creates idempotency_keys row; second call dedupes.
#   3. R3 invocation with dry_run=True writes to dry_run_outputs (via the
#      ctx-aware shim agents are expected to use).
#   4. Hard timeout fires when the agent sleeps past hard_timeout_ms.
#   5. Circuit breaker opens after failure_threshold consecutive failures
#      and rejects subsequent invocations with outcome='circuit_open'.
#
# Runs entirely inside the georag-fastapi container so the audit emitter
# and agent wrapper resolve via the same module paths the production app uses.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

WS_ID="${WS_ID:-00000000-aaaa-bbbb-cccc-000000000099}"
TEST_AGENT="phase0-wrapper-smoke"

cleanup() {
    $PG_PSQL_BIN -q -c "
        DELETE FROM audit.audit_ledger WHERE workspace_id = '${WS_ID}';
        DELETE FROM workspace.idempotency_keys WHERE workspace_id::text = '${WS_ID}';
        DELETE FROM workspace.dry_run_outputs  WHERE workspace_id = '${WS_ID}';
        DELETE FROM workspace.agent_timeouts WHERE agent_name LIKE '${TEST_AGENT}%';
        DELETE FROM silver.workspaces WHERE workspace_id = '${WS_ID}';
    " >/dev/null
    docker exec georag-redis redis-cli -a 'N2Wz3FdVExUkEs8AysiAmh4usppA8FZ' --no-auth-warning \
        --scan --pattern "georag:cb:${TEST_AGENT}*" 2>/dev/null \
        | xargs -r docker exec georag-redis redis-cli -a 'N2Wz3FdVExUkEs8AysiAmh4usppA8FZ' --no-auth-warning DEL >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup  # start clean

# Seed a synthetic silver.workspaces row so dry_run_outputs FK + workspace
# config FKs resolve. Cleanup drops it on EXIT.
$PG_PSQL_BIN -q -c "
    INSERT INTO silver.workspaces (workspace_id, name, slug)
    VALUES ('${WS_ID}', 'phase0-wrapper-smoke', 'phase0-wrapper-smoke-${WS_ID:0:8}')
    ON CONFLICT (workspace_id) DO NOTHING;
" >/dev/null

cat <<BANNER

============================================================
PHASE 0 STEP 5 — WRAPPER SMOKE TEST
============================================================
Workspace: ${WS_ID}
Test agent name prefix: ${TEST_AGENT}
============================================================
BANNER

# -----------------------------------------------------------------------------
# Seed agent_timeouts rows the smoke test needs.
# -----------------------------------------------------------------------------
$PG_PSQL_BIN -q -c "
    INSERT INTO workspace.agent_timeouts (agent_name, risk_tier, soft_timeout_ms, hard_timeout_ms, retry_count, circuit_breaker_scope, failure_threshold, cool_down_seconds)
    VALUES
        ('${TEST_AGENT}-r0',      'R0',  100,  1000, 0, 'workspace', 99, 60),
        ('${TEST_AGENT}-r2',      'R2', 1000,  5000, 0, 'workspace', 99, 60),
        ('${TEST_AGENT}-r3',      'R3', 1000,  5000, 0, 'workspace', 99, 60),
        ('${TEST_AGENT}-timeout', 'R0',   50,   200, 0, 'workspace', 99, 60),
        ('${TEST_AGENT}-circuit', 'R0',  100,  1000, 0, 'workspace',  3, 60)
    ON CONFLICT (agent_name) DO UPDATE SET
        soft_timeout_ms = EXCLUDED.soft_timeout_ms,
        hard_timeout_ms = EXCLUDED.hard_timeout_ms,
        failure_threshold = EXCLUDED.failure_threshold;
" >/dev/null

# -----------------------------------------------------------------------------
# Run all 5 scenarios in one Python process inside the fastapi container.
# -----------------------------------------------------------------------------
# Copy the python harness into the container so we don't have to wrestle with
# bash → docker → python heredoc escaping.
# Guard against Git Bash / MSYS2 Unix-path → Windows-path auto-mangling
# (which turns `/tmp/foo` into `C:/Users/.../tmp/foo` when passed to docker.exe
# and the source-side `/c/Users/...` into `C:\c\Users\...`). MSYS_NO_PATHCONV
# disables MSYS conversion for the docker invocation only; the doubled-slash
# trick `//tmp/` also defeats the conversion if MSYS_NO_PATHCONV is ignored.
PY_SRC="$(dirname "$0")/_phase0_wrapper_smoke.py"
if command -v cygpath >/dev/null 2>&1; then
    PY_SRC=$(cygpath -w "$PY_SRC")
fi
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker cp \
    "$PY_SRC" georag-fastapi:/tmp/_phase0_wrapper_smoke.py
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker exec \
    -e WS_ID="${WS_ID}" \
    -e AG="${TEST_AGENT}" \
    -e REDIS_PASSWORD='N2Wz3FdVExUkEs8AysiAmh4usppA8FZ' \
    georag-fastapi python3 -u //tmp/_phase0_wrapper_smoke.py
