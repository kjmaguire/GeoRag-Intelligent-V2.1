#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_acceptance.sh
#
# Phase 0 master acceptance harness (kickoff §Step 8). Runs:
#
#   - All per-step verifiers in sequence (Steps 1–6)
#   - The master plan §30 e2e test (workflow_run + Tempo span tree + audit
#     hash chain verification, end-to-end)
#   - Cross-cutting checks (each Phase 0 agent has emitted at least one
#     audit.invoke.success row in the last 2 hours, etc.)
#
# Phase 0 is "done" when every check in this harness passes.
#
# Exit code 0 = ready for Phase 1.
# =============================================================================

set -uo pipefail

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

# Source the env shim — exports PG_PSQL_BIN, FASTAPI_PYTHON_BIN, *_URL.
# Works from host (uses `docker exec`) or from inside a georag-network
# container (uses in-network DNS names directly). #17 unblocker.
# shellcheck source=lib/phase0_env.sh
. "${PROJECT_ROOT}/scripts/lib/phase0_env.sh"
echo "Phase 0 env mode: ${PHASE0_MODE}"

PASS=0
TOTAL=0
FAIL_REASONS=()

check() {
    TOTAL=$((TOTAL + 1))
    if [ "$2" = "ok" ]; then
        echo "  [PASS] $1"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $1 — $3"
        FAIL_REASONS+=("$1")
    fi
}

cat <<'BANNER'

============================================================
PHASE 0 — MASTER ACCEPTANCE (kickoff §Step 8)
============================================================
BANNER
echo "Working dir: $(pwd)"
echo "Started:     $(date -u +%FT%TZ)"
echo

# -----------------------------------------------------------------------------
# A) Per-step verifiers (Steps 1–6)
# -----------------------------------------------------------------------------
echo "------------------------------------------------------------"
echo "A) Per-step verifiers"
echo "------------------------------------------------------------"
for s in 1 2 3 4 5 6; do
    script="scripts/phase0_step${s}_verify.sh"
    if [ ! -x "$script" ]; then
        check "Step ${s} verifier present" fail "missing $script"
        continue
    fi
    out=$(bash "$script" 2>&1)
    last=$(echo "$out" | grep -E '^Result: ' | tail -1)
    if [[ "$last" =~ Result:\ ([0-9]+)\ /\ ([0-9]+) ]]; then
        if [ "${BASH_REMATCH[1]}" = "${BASH_REMATCH[2]}" ]; then
            check "Step ${s} (${BASH_REMATCH[1]}/${BASH_REMATCH[2]} checks)" ok
        else
            check "Step ${s} (${BASH_REMATCH[1]}/${BASH_REMATCH[2]} checks)" fail \
                "$(echo "$out" | grep -E '\[FAIL\]' | head -3 | tr '\n' '; ')"
        fi
    else
        check "Step ${s}" fail "could not parse Result line"
    fi
done

# -----------------------------------------------------------------------------
# B) Master plan §30 end-to-end
# -----------------------------------------------------------------------------
echo
echo "------------------------------------------------------------"
echo "B) Master plan §30 end-to-end (workflow_runs + Tempo + audit chain)"
echo "------------------------------------------------------------"

E2E_OUT=$(bash scripts/phase0_acceptance_e2e.sh 2>&1)
E2E_EXIT=$?
TRACE_ID=$(echo "$E2E_OUT" | tail -1 | tr -d '[:space:]')

if [ "$E2E_EXIT" != "0" ] || [ -z "$TRACE_ID" ] || [ "${#TRACE_ID}" -ne 32 ]; then
    check "phase0_acceptance_e2e.sh ran" fail "exit=$E2E_EXIT trace_id='$TRACE_ID'"
else
    check "phase0_acceptance_e2e.sh ran (trace_id=${TRACE_ID:0:8}…)" ok
fi

if [ -n "$TRACE_ID" ] && [ "${#TRACE_ID}" -eq 32 ]; then
    sleep 6  # let Tempo flush

    # B1) workflow_runs row exists with that trace_id
    n=$($PG_PSQL_BIN -tAc \
        "SELECT count(*) FROM workflow.workflow_runs WHERE trace_id = '$TRACE_ID' AND status = 'success';" | tr -d ' ')
    if [ "$n" = "1" ]; then
        check "workflow.workflow_runs has 1 row with trace_id (status=success)" ok
    else
        check "workflow_runs row" fail "got $n"
    fi

    # B2) Tempo serves the span tree
    batches=$(curl -s "${TEMPO_URL}/api/traces/${TRACE_ID}" \
        | python3 -c 'import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(len(d.get("batches", d.get("traces", []))))
except Exception:
    print(0)' 2>/dev/null)
    if [ "${batches:-0}" -ge 1 ] 2>/dev/null; then
        check "Tempo serves the trace (${batches} batch(es))" ok
    else
        check "Tempo trace fetch" fail "got 0 batches"
    fi

    # B3) audit_ledger entries with that trace_id (≥3 from the e2e script)
    n_audit=$($PG_PSQL_BIN -tAc \
        "SELECT count(*) FROM audit.audit_ledger WHERE trace_id = '$TRACE_ID';" | tr -d ' ')
    if [ "$n_audit" -ge 3 ] 2>/dev/null; then
        check "audit_ledger has ≥3 rows with trace_id (got $n_audit)" ok
    else
        check "audit_ledger trace rows" fail "got $n_audit"
    fi

    # B4) Verify the e2e workspace's chain in isolation.
    #
    # We don't use audit.run_verification here because its windowed LAG()
    # produces false-breaks on the first row in the window (its stored
    # previous_hash points to a row outside the window). The acceptance
    # workspace is fresh — its chain starts from the e2e write, so a direct
    # full-chain recompute against just that workspace is unambiguous.
    # Phase 11 will fix run_verification to look up out-of-window predecessors.
    e2e_ws="00000000-acce-ed30-cccc-000000000030"
    chain_breaks=$($PG_PSQL_BIN -tAc "
        WITH ordered AS (
            SELECT id, actor_id, actor_kind, action_type, target_schema,
                   target_table, target_id, payload, previous_hash, hash, created_at,
                   LAG(hash) OVER (ORDER BY created_at, id) AS expected_prev,
                   ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn
            FROM audit.audit_ledger
            WHERE workspace_id = '${e2e_ws}'
        )
        SELECT count(*) FROM ordered o
        WHERE rn > 1
          AND (
              o.previous_hash IS DISTINCT FROM o.expected_prev
              OR o.hash IS DISTINCT FROM audit.recompute_hash(
                    o.expected_prev, o.actor_id, o.actor_kind, o.action_type,
                    o.target_schema, o.target_table, o.target_id, o.payload,
                    o.created_at)
          );" | tr -d ' ')
    if [ "$chain_breaks" = "0" ]; then
        check "e2e workspace chain integrity (0 breaks)" ok
    else
        check "audit chain integrity" fail "got $chain_breaks chain break(s) in e2e workspace"
    fi
fi

# -----------------------------------------------------------------------------
# C) Cross-cutting Phase 0 health
# -----------------------------------------------------------------------------
echo
echo "------------------------------------------------------------"
echo "C) Cross-cutting Phase 0 health"
echo "------------------------------------------------------------"

# C1) Invoke the 4 Phase 0 agents implemented in this main session against
#     a synthetic workspace, then assert the audit trail. This proves each
#     agent works end-to-end without depending on prior smoke runs (whose
#     cleanup hooks delete the audit rows).
ACCEPTANCE_WS_ID="00000000-acc6-eea4-cccc-111111110001"
$PG_PSQL_BIN -q -c "
    INSERT INTO silver.workspaces (workspace_id, name, slug)
    VALUES ('${ACCEPTANCE_WS_ID}', 'phase0-acceptance-c1',
            'phase0-acc-c1-${ACCEPTANCE_WS_ID:0:8}')
    ON CONFLICT (workspace_id) DO NOTHING;
" >/dev/null

export WS_ID="$ACCEPTANCE_WS_ID"
export REDIS_PASSWORD="${REDIS_PASSWORD:-N2Wz3FdVExUkEs8AysiAmh4usppA8FZ}"
agent_invoke_out=$(fastapi_python_with_env WS_ID REDIS_PASSWORD -- -c "
import asyncio, asyncpg, os, sys, uuid
import redis.asyncio as aioredis
sys.path.insert(0, '/app')
from app.agents import register_runtime, AgentContext
from app.agents.phase0 import (
    tenant_isolation_audit, lineage_walk,
    index_health_check, store_reconciliation_run,
)

async def main():
    pool = await asyncpg.create_pool('postgres://'+os.environ.get('POSTGRES_USER','georag')+':'+os.environ['POSTGRES_PASSWORD']+'@postgresql:5432/'+os.environ.get('POSTGRES_DB','georag'), min_size=1, max_size=4, statement_cache_size=0)
    redis = aioredis.from_url('redis://:'+os.environ['REDIS_PASSWORD']+'@redis:6379/0', decode_responses=True)
    register_runtime(pg_pool=pool, redis=redis)
    ws = uuid.UUID(os.environ['WS_ID'])
    outcomes = []
    for r in await asyncio.gather(
        tenant_isolation_audit(ctx=AgentContext(workspace_id=ws), probes_per_table=1),
        lineage_walk(ctx=AgentContext(workspace_id=ws), target_type='workspace', target_id=str(ws), limit=10),
        index_health_check(ctx=AgentContext(workspace_id=ws), slow_query_ms_threshold=99999.0),
        store_reconciliation_run(ctx=AgentContext(workspace_id=ws)),
    ):
        outcomes.append(r.outcome)
    await pool.close(); await redis.close()
    print(','.join(outcomes))

asyncio.run(main())
" 2>&1 | tail -1)

if [ "$agent_invoke_out" = "success,success,success,success" ]; then
    check "All 4 Phase 0 agents invoked successfully (live invocation)" ok
else
    check "4-agent live invocation" fail "outcomes=$agent_invoke_out"
fi

n_audited=$($PG_PSQL_BIN -tAc "
    SELECT count(DISTINCT payload->>'agent_name')
    FROM audit.audit_ledger
    WHERE action_type LIKE 'agent.invoke.%'
      AND workspace_id = '${ACCEPTANCE_WS_ID}'
      AND created_at > now() - interval '5 minutes'
      AND payload->>'agent_name' IN (
        'Tenant Isolation Auditor','Lineage Reporter Agent',
        'Index Health Agent','Store Reconciliation Agent'
      );" | tr -d ' ')
[ "$n_audited" = "4" ] && check "All 4 agents emitted agent.invoke audit rows (4/4)" ok \
    || check "agent audit rows" fail "got $n_audited/4"

# Cleanup the synthetic acceptance workspace.
$PG_PSQL_BIN -q -c "
    DELETE FROM audit.audit_ledger WHERE workspace_id = '${ACCEPTANCE_WS_ID}';
    DELETE FROM silver.store_reconciliation_findings WHERE workspace_id = '${ACCEPTANCE_WS_ID}';
    DELETE FROM silver.corpus_health_findings WHERE workspace_id = '${ACCEPTANCE_WS_ID}';
    DELETE FROM silver.workspaces WHERE workspace_id = '${ACCEPTANCE_WS_ID}';
" >/dev/null

# C2) pg_partman partitioning is still tracking (Step 1+2 sanity)
parts=$($PG_PSQL_BIN -tAc \
    "SELECT count(*) FROM partman.part_config WHERE parent_table IN ('audit.audit_ledger','workflow.workflow_runs','usage.usage_events');" | tr -d ' ')
[ "$parts" = "3" ] && check "pg_partman tracking 3 partitioned parents" ok || check "partman tracking" fail "got $parts/3"

# C3) Hatchet engine is healthy
hatchet_code=$(curl -s -o /dev/null -w '%{http_code}' "${HATCHET_URL}/api/ready" 2>/dev/null || echo 000)
[ "$hatchet_code" = "200" ] && check "Hatchet /api/ready → 200" ok || check "Hatchet" fail "$hatchet_code"

# C4) OTel collector is healthy
otel_code=$(curl -s -o /dev/null -w '%{http_code}' "${OTEL_HEALTH_URL}/" 2>/dev/null || echo 000)
[ "$otel_code" = "200" ] && check "OTel collector /health → 200" ok || check "OTel" fail "$otel_code"

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo
echo "============================================================"
echo "Aggregate: ${PASS} / ${TOTAL} acceptance checks passed"
if [ ${#FAIL_REASONS[@]} -gt 0 ]; then
    echo
    echo "Failures:"
    for f in "${FAIL_REASONS[@]}"; do echo "  - $f"; done
fi
echo "============================================================"
echo
echo "Finished: $(date -u +%FT%TZ)"

[ ${#FAIL_REASONS[@]} -eq 0 ]
