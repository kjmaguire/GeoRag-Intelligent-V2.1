-- =============================================================================
-- Phase 0 — Row-Level Security policies
--
-- Every workspace-scoped Phase 0 table gets RLS enforcing
--   workspace_id = current_setting('app.workspace_id')::uuid
-- with a fallback that the application MUST set the GUC before queries.
--
-- The `app.workspace_id` GUC is set per-connection (or per-transaction) by
-- the application middleware. PgBouncer + asyncpg pattern:
--   await conn.execute("SET LOCAL app.workspace_id = $1", workspace_id)
--
-- Tables that get RLS (per kickoff §Step 2):
--   workspace.workspace_memberships, workspace.workspace_roles
--   workspace.workspace_agent_config, workspace.idempotency_keys, workspace.dry_run_outputs
--   audit.audit_ledger
--   (audit.audit_ledger_verification_runs intentionally omitted — system-wide
--    table with no workspace_id column; admin-RBAC-gated at the app layer.
--    Spec inconsistency in kickoff §Step 2; surface for v2.4.3 doc revision.)
--   workflow.workflow_runs, workflow.workflow_run_events
--   outbox.pending_propagations, outbox.propagation_attempts
--   usage.usage_events, usage.usage_aggregates_daily, usage.workspace_cost_ceilings
--   silver.store_reconciliation_findings, silver.corpus_health_findings, silver.storage_tier_policy
--
-- Tables that DO NOT get RLS:
--   public.users (cross-workspace identity)
--   silver.workspaces (members read; only admins write — handled at app layer)
--   workspace.prompt_versions, workspace.agent_timeouts, workspace.agent_prompt_pins (global config)
--   audit.integration_credentials_audit (admin-only — gated by RBAC, not RLS)
-- =============================================================================

-- Helper macro: enable RLS + add a workspace_id-based policy.
-- Existing policies are dropped + recreated so the script is re-runnable.
DO $$
DECLARE
    target_tables text[][] := ARRAY[
        ARRAY['workspace', 'workspace_memberships'],
        ARRAY['workspace', 'workspace_agent_config'],
        ARRAY['workspace', 'idempotency_keys'],
        ARRAY['workspace', 'dry_run_outputs'],
        ARRAY['audit',     'audit_ledger'],
        -- audit.audit_ledger_verification_runs has no workspace_id; skip RLS.
        ARRAY['workflow',  'workflow_runs'],
        ARRAY['workflow',  'workflow_run_events'],
        ARRAY['outbox',    'pending_propagations'],
        ARRAY['outbox',    'propagation_attempts'],
        ARRAY['usage',     'usage_events'],
        ARRAY['usage',     'usage_aggregates_daily'],
        ARRAY['usage',     'workspace_cost_ceilings'],
        ARRAY['silver',    'store_reconciliation_findings'],
        ARRAY['silver',    'corpus_health_findings'],
        ARRAY['silver',    'storage_tier_policy']
    ];
    s text;
    t text;
    qualified text;
    i int;
BEGIN
    FOR i IN 1 .. array_length(target_tables, 1) LOOP
        s := target_tables[i][1];
        t := target_tables[i][2];
        qualified := format('%I.%I', s, t);

        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', qualified);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY',  qualified);

        -- Drop any existing tenant policy so this script is idempotent
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', qualified);

        -- Workspace-scoped read+write policy: workspace_id must match the GUC.
        -- NULL workspace_id (system-wide events) is visible only when GUC is unset
        -- or explicitly NULL — the verifier and admin paths run with no GUC set.
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %s
                USING (
                    workspace_id IS NOT DISTINCT FROM
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    OR current_setting('app.workspace_id', true) IS NULL
                    OR current_setting('app.workspace_id', true) = ''
                )
                WITH CHECK (
                    workspace_id IS NOT DISTINCT FROM
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    OR current_setting('app.workspace_id', true) IS NULL
                    OR current_setting('app.workspace_id', true) = ''
                )
        $f$, qualified);

        RAISE NOTICE 'RLS enabled + tenant_isolation policy applied: %', qualified;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- workspace_roles is a special case: rows with workspace_id = NULL are global
-- system roles visible to everyone. Per-workspace roles follow the tenant
-- policy. Keep the policy simpler so the system-role lookup at session start
-- is unambiguous.
-- ---------------------------------------------------------------------------
ALTER TABLE workspace.workspace_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.workspace_roles FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workspace_roles_visibility ON workspace.workspace_roles;
CREATE POLICY workspace_roles_visibility ON workspace.workspace_roles
    USING (
        workspace_id IS NULL
        OR workspace_id IS NOT DISTINCT FROM
           NULLIF(current_setting('app.workspace_id', true), '')::uuid
        OR current_setting('app.workspace_id', true) IS NULL
        OR current_setting('app.workspace_id', true) = ''
    )
    WITH CHECK (
        workspace_id IS NOT DISTINCT FROM
            NULLIF(current_setting('app.workspace_id', true), '')::uuid
        OR current_setting('app.workspace_id', true) IS NULL
        OR current_setting('app.workspace_id', true) = ''
    );
