#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_acceptance_e2e.sh
#
# Master plan §30 Phase 0 done definition — end-to-end test workflow:
#
#   "A test workflow run appears in workflow_runs with full span tree in
#    Tempo, and audit ledger entries verify their own hash chain."
#
# This script:
#   1. Generates a fresh trace_id (32 hex chars).
#   2. Emits 5 OTLP spans through the OTel collector → Tempo with that
#      trace_id (parent + 4 children to form a span tree).
#   3. INSERTs a workflow_runs row (workflow_kind='phase0_acceptance_e2e',
#      engine='hatchet', status='success') stamped with the same trace_id.
#   4. Calls the Python audit emitter inside georag-fastapi to write 3
#      audit_ledger entries linked to that workflow_runs row.
#   5. Runs audit.run_verification() over the past 5 minutes.
#   6. Prints just the trace_id on the final line of stdout — callers can
#      capture it with TRACE_ID=$(./scripts/phase0_acceptance_e2e.sh | tail -1).
#
# Cleanup of the synthetic rows happens at the end of phase0_acceptance.sh,
# not here, so the dashboard can be inspected manually after a run.
# =============================================================================

set -uo pipefail

# Source the env shim. Picks host (docker exec) vs container (in-network)
# mode automatically. #17 unblocker.
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

WS_ID="${WS_ID:-00000000-acce-ed30-cccc-000000000030}"
RUN_ID=$($PG_PSQL_BIN -tAc "SELECT gen_random_uuid();" | tr -d ' \r')
TRACE_ID=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || \
           $FASTAPI_PYTHON_BIN -c "import secrets; print(secrets.token_hex(16))" | tr -d '\r')

echo "==> Phase 0 §30 acceptance e2e"
echo "    trace_id = $TRACE_ID"
echo "    run_id   = $RUN_ID"
echo "    ws_id    = $WS_ID"

# -----------------------------------------------------------------------------
# 1) Seed silver.workspaces row for FK satisfaction.
# -----------------------------------------------------------------------------
$PG_PSQL_BIN -q -c "
    INSERT INTO silver.workspaces (workspace_id, name, slug)
    VALUES ('${WS_ID}', 'phase0-acceptance', 'phase0-acceptance-${WS_ID:0:12}')
    ON CONFLICT (workspace_id) DO NOTHING;
" >/dev/null

# -----------------------------------------------------------------------------
# 2) Emit 5 OTLP spans (parent + 4 children) via the collector → Tempo.
# -----------------------------------------------------------------------------
echo "==> Emitting span tree (1 parent + 4 children) to OTel collector"
export TRACE_ID
fastapi_python_with_env TRACE_ID -- -c "
import json, os, secrets, time, urllib.request

trace_id = os.environ['TRACE_ID']
parent_span = secrets.token_hex(8)
now_ns = int(time.time() * 1e9)

def span(name, span_id, parent_id, start_offset_ns, duration_ns, kind=1):
    return {
        'traceId': trace_id, 'spanId': span_id,
        'parentSpanId': parent_id, 'name': name,
        'kind': kind,
        'startTimeUnixNano': str(now_ns + start_offset_ns),
        'endTimeUnixNano':   str(now_ns + start_offset_ns + duration_ns),
        'status': {'code': 1},
        'attributes': [
            {'key': 'phase',  'value': {'stringValue': '0'}},
            {'key': 'agent',  'value': {'stringValue': 'phase0-acceptance'}},
        ],
    }

children = [
    span('schema.lookup',    secrets.token_hex(8), parent_span,   1_000_000,   2_000_000),
    span('llm.invoke',       secrets.token_hex(8), parent_span,   3_000_000,   8_000_000),
    span('audit.emit',       secrets.token_hex(8), parent_span,  11_000_000,   1_000_000),
    span('outbox.enqueue',   secrets.token_hex(8), parent_span,  12_500_000,   1_000_000),
]
spans = [
    span('phase0_acceptance_e2e.workflow', parent_span, '', 0, 14_000_000),
    *children,
]

payload = {
    'resourceSpans': [{
        'resource': {'attributes': [
            {'key': 'service.name', 'value': {'stringValue': 'phase0-acceptance'}},
            {'key': 'deployment.environment', 'value': {'stringValue': 'dev'}},
        ]},
        'scopeSpans': [{
            'scope': {'name': 'phase0_acceptance_e2e'},
            'spans': spans,
        }],
    }],
}

req = urllib.request.Request(
    'http://otel-collector:4318/v1/traces',
    data=json.dumps(payload).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=5) as r:
    print(f'  OTLP HTTP → {r.status}, {len(spans)} spans posted')
"

# -----------------------------------------------------------------------------
# 3) INSERT workflow_runs row with the trace_id.
# -----------------------------------------------------------------------------
echo "==> INSERT workflow_runs row"
$PG_PSQL_BIN -q -c "
    INSERT INTO workflow.workflow_runs
        (run_id, workspace_id, workflow_kind, engine, engine_run_id,
         status, trace_id, started_at, ended_at, input_summary, output_summary)
    VALUES
        ('${RUN_ID}', '${WS_ID}', 'phase0_acceptance_e2e', 'hatchet',
         'phase0-${RUN_ID:0:8}', 'success', '${TRACE_ID}',
         now() - interval '14 ms', now(), '{}'::jsonb, '{\"acceptance\": true}'::jsonb);
" >/dev/null

# -----------------------------------------------------------------------------
# 4) Emit 3 audit_ledger entries via the Python emitter, linked to the run.
# -----------------------------------------------------------------------------
echo "==> Emit 3 audit_ledger entries via Python emitter"
export TRACE_ID RUN_ID WS_ID
fastapi_python_with_env TRACE_ID RUN_ID WS_ID -- -c "
import asyncio, asyncpg, os, sys
sys.path.insert(0, '/app')
from app.audit import emit_audit
from uuid import UUID

async def main():
    dsn = 'postgres://'+os.environ.get('POSTGRES_USER','georag')+':'+os.environ['POSTGRES_PASSWORD']+'@postgresql:5432/'+os.environ.get('POSTGRES_DB','georag')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, statement_cache_size=0)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for i, action in enumerate(['workflow.start', 'workflow.step.complete', 'workflow.end']):
                    await emit_audit(
                        conn,
                        action_type=action,
                        workspace_id=UUID(os.environ['WS_ID']),
                        actor_kind='workflow',
                        target_schema='workflow',
                        target_table='workflow_runs',
                        target_id=os.environ['RUN_ID'],
                        payload={'step': i, 'kind': 'phase0_acceptance_e2e'},
                        trace_id=os.environ['TRACE_ID'],
                    )
        print('  ✓ 3 audit entries emitted')
    finally:
        await pool.close()

asyncio.run(main())
"

# -----------------------------------------------------------------------------
# 5) Run audit verification over the recent window.
# -----------------------------------------------------------------------------
echo "==> Run audit.run_verification() over last 5 minutes"
verify_run_id=$($PG_PSQL_BIN -tAc "
    SELECT audit.run_verification(
        now() - interval '5 minutes',
        now() + interval '1 minute'
    );" | tr -d ' ')
verify_status=$($PG_PSQL_BIN -tAc "
    SELECT status || '|' || rows_verified
    FROM audit.audit_ledger_verification_runs WHERE id = '${verify_run_id}';" | tr -d ' ')
echo "  verify_run_id=${verify_run_id:0:8}… status=${verify_status}"

# Final line of stdout = trace_id (for caller capture).
echo
echo "${TRACE_ID}"
