#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_step2_verify.sh
#
# Phase 0 Step 2 done-definition (per georag-phase0-implementation-kickoff.md).
# Exits 0 only if all checks pass.
#
# Spec deviations vs kickoff doc (logged in project memory):
#   - Layer A: silver.workspaces + public.users are existing canonical tables
#     (not workspace.workspaces / workspace.users). Only workspace_memberships
#     and workspace_roles are net-new. Total table count: 22, not 24.
#   - audit.audit_ledger_verification_runs has no workspace_id; RLS skipped.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

PASS=0
TOTAL=8

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

q() {
    $PG_PSQL_BIN -tAc "$1" 2>/dev/null
}

cat <<'BANNER'

============================================================
PHASE 0 STEP 2 — DONE-DEFINITION VERIFICATION
============================================================
BANNER

# 1) All Phase 0 Step 2 tables present (22 net-new + 2 pre-existing alias = 24 logical).
#
# Net-new tables we created in Step 2:
#   workspace.workspace_memberships
#   workspace.workspace_roles
#   workspace.agent_timeouts
#   workspace.prompt_versions
#   workspace.agent_prompt_pins
#   workspace.workspace_agent_config
#   workspace.idempotency_keys
#   workspace.dry_run_outputs
#   audit.audit_ledger
#   audit.audit_ledger_verification_runs
#   audit.integration_credentials_audit
#   workflow.workflow_runs
#   workflow.workflow_run_events
#   outbox.pending_propagations
#   outbox.propagation_attempts
#   usage.usage_events
#   usage.usage_aggregates_daily
#   usage.workspace_cost_ceilings
#   silver.store_reconciliation_findings
#   silver.corpus_health_findings
#   silver.storage_tier_policy
# Total: 21 net-new

new_tables=$(q "
SELECT count(*)
FROM information_schema.tables
WHERE (table_schema, table_name) IN (
  ('workspace','workspace_memberships'),('workspace','workspace_roles'),
  ('workspace','agent_timeouts'),('workspace','prompt_versions'),
  ('workspace','agent_prompt_pins'),('workspace','workspace_agent_config'),
  ('workspace','idempotency_keys'),('workspace','dry_run_outputs'),
  ('audit','audit_ledger'),('audit','audit_ledger_verification_runs'),
  ('audit','integration_credentials_audit'),
  ('workflow','workflow_runs'),('workflow','workflow_run_events'),
  ('outbox','pending_propagations'),('outbox','propagation_attempts'),
  ('usage','usage_events'),('usage','usage_aggregates_daily'),('usage','workspace_cost_ceilings'),
  ('silver','store_reconciliation_findings'),('silver','corpus_health_findings'),('silver','storage_tier_policy')
);")
new_tables="${new_tables// /}"
[ "$new_tables" = "21" ] && check "21/21 net-new Phase 0 tables present" ok || check "Phase 0 tables" fail "got $new_tables / 21"

# 2) silver.workspaces + public.users still present (Layer A canonical)
existing=$(q "SELECT count(*) FROM information_schema.tables WHERE (table_schema, table_name) IN (('silver','workspaces'),('public','users'));")
existing="${existing// /}"
[ "$existing" = "2" ] && check "silver.workspaces + public.users (Layer A canonical) still present" ok || check "Layer A canonical" fail "got $existing / 2"

# 3) pg_partman registered for the three time-partitioned tables
pman=$(q "SELECT count(*) FROM partman.part_config WHERE parent_table IN ('audit.audit_ledger','workflow.workflow_runs','usage.usage_events');")
pman="${pman// /}"
[ "$pman" = "3" ] && check "pg_partman tracking 3 time-partitioned parents" ok || check "pg_partman" fail "got $pman / 3"

# 4) At least 3 monthly child partitions exist for each partitioned parent
parts_ok=$(q "
SELECT bool_and(part_count >= 3)::text FROM (
  SELECT parent_table, count(*) AS part_count
  FROM (
    SELECT 'audit.audit_ledger'      AS parent_table, partition_tablename FROM partman.show_partitions('audit.audit_ledger')
    UNION ALL
    SELECT 'workflow.workflow_runs', partition_tablename FROM partman.show_partitions('workflow.workflow_runs')
    UNION ALL
    SELECT 'usage.usage_events',     partition_tablename FROM partman.show_partitions('usage.usage_events')
  ) s
  GROUP BY parent_table
) g;")
parts_ok="${parts_ok// /}"
[ "$parts_ok" = "t" ] || [ "$parts_ok" = "true" ] && check "≥3 monthly partitions exist for audit_ledger / workflow_runs / usage_events" ok || check "partition count" fail "got $parts_ok"

# 5) RLS enabled on the 16 workspace-scoped Phase 0 tables
rls_count=$(q "
SELECT count(*) FROM pg_tables
WHERE rowsecurity = true
AND (schemaname, tablename) IN (
  ('workspace','workspace_memberships'),('workspace','workspace_roles'),
  ('workspace','workspace_agent_config'),('workspace','idempotency_keys'),('workspace','dry_run_outputs'),
  ('audit','audit_ledger'),
  ('workflow','workflow_runs'),('workflow','workflow_run_events'),
  ('outbox','pending_propagations'),('outbox','propagation_attempts'),
  ('usage','usage_events'),('usage','usage_aggregates_daily'),('usage','workspace_cost_ceilings'),
  ('silver','store_reconciliation_findings'),('silver','corpus_health_findings'),('silver','storage_tier_policy')
);")
rls_count="${rls_count// /}"
[ "$rls_count" = "16" ] && check "RLS enabled on 16 workspace-scoped tables" ok || check "RLS" fail "got $rls_count / 16"

# 6) Audit hash-chain trigger present + functional (genesis row exists with non-null hash)
genesis=$(q "SELECT (hash IS NOT NULL)::text FROM audit.audit_ledger WHERE action_type = 'audit_ledger.genesis' LIMIT 1;")
genesis="${genesis// /}"
[ "$genesis" = "t" ] || [ "$genesis" = "true" ] && check "audit_ledger genesis row inserted with hash computed" ok || check "audit hash chain" fail "got $genesis"

# 7) Audit hash chain links: insert two rows under a fake workspace and verify chain
chain_ok=$(q "
DO \$\$
DECLARE
  test_ws uuid := '00000000-0000-0000-0000-000000000001';
  h1 bytea; h2 bytea; prev2 bytea;
BEGIN
  INSERT INTO audit.audit_ledger (workspace_id, action_type, payload)
    VALUES (test_ws, 'phase0.verify.test1', '{\"i\":1}'::jsonb)
    RETURNING hash INTO h1;
  INSERT INTO audit.audit_ledger (workspace_id, action_type, payload)
    VALUES (test_ws, 'phase0.verify.test2', '{\"i\":2}'::jsonb)
    RETURNING hash, previous_hash INTO h2, prev2;
  IF h1 IS NULL OR h2 IS NULL OR prev2 IS DISTINCT FROM h1 THEN
    RAISE EXCEPTION 'hash chain broken (h1=% prev2=%)', h1, prev2;
  END IF;
END \$\$;
SELECT 'ok';"
)
chain_ok=$(echo "$chain_ok" | tail -1 | tr -d ' ')
[ "$chain_ok" = "ok" ] && check "audit_ledger hash chain links across 2 inserts" ok || check "hash chain link" fail "$chain_ok"

# 8) Seeded Phase 0 defaults present (>= 3 system roles + >= 5 storage tier
# policies). Counts grew in later phases — assert lower bounds, not exact,
# so this Phase 0 check stays valid as the seed set expands.
roles=$(q "SELECT count(*) FROM workspace.workspace_roles WHERE is_system;" | tr -d ' ')
tiers=$(q "SELECT count(*) FROM silver.storage_tier_policy WHERE workspace_id IS NULL;" | tr -d ' ')
if [ "${roles:-0}" -ge 3 ] && [ "${tiers:-0}" -ge 5 ]; then
    check "Seed data present (>=3 system roles, >=5 default tier policies; got $roles/$tiers)" ok
else
    check "seed data" fail "got $roles roles / $tiers tiers (expected >=3 / >=5)"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"
exit $((PASS == TOTAL ? 0 : 1))
