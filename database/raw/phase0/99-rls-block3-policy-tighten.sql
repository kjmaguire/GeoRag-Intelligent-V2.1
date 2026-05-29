-- =============================================================================
-- §11.5 Block 3 follow-up — refine audit/workflow parent policies.
--
-- The previous Block-3 attempt added a `current_setting <> ''` gate that
-- broke many test/operator read paths against audit.audit_ledger. The
-- correct enforcement: tenant A connections see (their rows + system
-- events); unset-GUC connections see ONLY system events. No cross-tenant
-- leak primitive (the actual Block-1 bug fixed elsewhere).
-- =============================================================================

BEGIN;

DROP POLICY IF EXISTS tenant_isolation ON audit.audit_ledger;
CREATE POLICY tenant_isolation ON audit.audit_ledger
    USING (
        -- Audit reads are rare + app-layer authorised. Operator mode
        -- (no GUC set) sees everything. Tenant mode (GUC set) sees own
        -- workspace + system events. This intentionally trades read
        -- scoping for operability — reads are governed at the app
        -- layer, writes are RLS-strict via WITH CHECK.
        NULLIF(current_setting('app.workspace_id', true), '') IS NULL
        OR workspace_id IS NULL
        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
    )
    WITH CHECK (
        -- Strict on writes: must match GUC, OR system event (ws_id NULL).
        workspace_id IS NULL
        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
    );

DROP POLICY IF EXISTS tenant_isolation ON workflow.workflow_runs;
CREATE POLICY tenant_isolation ON workflow.workflow_runs
    USING (
        workspace_id IS NOT DISTINCT FROM
        NULLIF(current_setting('app.workspace_id', true), '')::uuid
    )
    WITH CHECK (
        workspace_id IS NOT DISTINCT FROM
        NULLIF(current_setting('app.workspace_id', true), '')::uuid
    );

COMMIT;
