-- =============================================================================
-- §4 Tool Gateway — schema (doc-phase 183)
--
-- Closes the master-plan-audit-flagged foundational gap: the central
-- governance layer for the 19 approved agent tools (registry + risk tiers +
-- workspace permissions + approval requirements + dry-run capture).
--
-- Tables created:
--   workspace.agent_risk_tiers       19 registered tools + their R0-R5 tier
--   workspace.agent_permissions      per-workspace × tool allow/deny matrix
--   workspace.approval_requirements  per-workspace × tool required reviewer + threshold
--   workspace.tool_invocations       audit ring of every tool call (R0+ for now)
--
-- workspace.dry_run_outputs already exists (Phase 0 step 5) — wired through.
-- =============================================================================

-- 1. Agent risk-tier registry (global; not per-workspace).
CREATE TABLE IF NOT EXISTS workspace.agent_risk_tiers (
    tool_name        varchar(64) PRIMARY KEY,
    risk_tier        varchar(4) NOT NULL,
    description      text NOT NULL,
    requires_dry_run boolean NOT NULL DEFAULT FALSE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_risk_tier_valid CHECK (
        risk_tier IN ('R0','R1','R2','R3','R4','R5')
    )
);

-- 2. Per-workspace × tool allow/deny + override.
CREATE TABLE IF NOT EXISTS workspace.agent_permissions (
    workspace_id     uuid NOT NULL,
    tool_name        varchar(64) NOT NULL,
    allowed          boolean NOT NULL DEFAULT TRUE,
    override_tier    varchar(4),  -- optional: workspace bumps tier higher
    notes            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, tool_name),
    CONSTRAINT agent_perm_override_tier_valid CHECK (
        override_tier IS NULL OR override_tier IN ('R0','R1','R2','R3','R4','R5')
    )
);
CREATE INDEX IF NOT EXISTS idx_agent_perm_workspace
    ON workspace.agent_permissions (workspace_id);

-- 3. Per-tool approval requirements (R4+ tools).
CREATE TABLE IF NOT EXISTS workspace.approval_requirements (
    workspace_id     uuid NOT NULL,
    tool_name        varchar(64) NOT NULL,
    required_role    varchar(40) NOT NULL DEFAULT 'qp_signoff',  -- qp_signoff | admin | owner
    min_credentials  jsonb NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"qp_credential_verified": true}
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, tool_name)
);

-- 4. Tool invocation audit ring — every gateway dispatch lands here.
-- Distinct from audit.audit_ledger which carries only R3+ actions; this
-- table is the operational log for ALL gateway calls (debugging,
-- explainability, cost attribution).
CREATE TABLE IF NOT EXISTS workspace.tool_invocations (
    invocation_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL,
    actor_user_id    bigint,
    actor_kind       varchar(20) NOT NULL DEFAULT 'agent',  -- user | agent | workflow | system
    tool_name        varchar(64) NOT NULL,
    risk_tier        varchar(4) NOT NULL,
    outcome          varchar(20) NOT NULL,  -- allowed | dry_run | blocked | error
    block_reason     text,
    parent_run_id    uuid,                -- workflow_run_id when triggered from a workflow
    trace_id         varchar(64),
    input_hash       varchar(64),          -- sha256 of canonical-json inputs
    output_hash      varchar(64),
    duration_ms      integer,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tool_invocation_outcome_valid CHECK (
        outcome IN ('allowed','dry_run','blocked','error')
    )
);
CREATE INDEX IF NOT EXISTS idx_tool_inv_workspace
    ON workspace.tool_invocations (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_inv_tool
    ON workspace.tool_invocations (tool_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_inv_parent
    ON workspace.tool_invocations (parent_run_id) WHERE parent_run_id IS NOT NULL;

-- =============================================================================
-- Seed the 19 approved tools per §4.2 + risk tiers per §4.3
-- =============================================================================
INSERT INTO workspace.agent_risk_tiers (tool_name, risk_tier, description, requires_dry_run) VALUES
    ('start_ingestion',                'R2', 'Kick off a Hatchet ingestion run for a workspace.',                 FALSE),
    ('validate_schema',                'R1', 'Suggest vendor → canonical column mappings.',                       FALSE),
    ('audit_provenance',               'R0', 'Read silver.* provenance chain for a row.',                          FALSE),
    ('query_postgis_readonly',         'R0', 'Read-only PostGIS query against silver/gold/public_geo.',     FALSE),
    ('query_neo4j_readonly',           'R0', 'Read-only Cypher against the workspace graph.',                      FALSE),
    ('retrieve_qdrant',                'R0', 'Vector search against workspace + public Qdrant collections.',       FALSE),
    ('trigger_activepieces_flow',      'R3', 'Fire an external integration flow (Kestra in this build).',          TRUE),
    ('dispatch_hatchet_workflow',      'R2', 'Dispatch a registered Hatchet workflow.',                            FALSE),
    ('trigger_dagster_asset',          'R2', 'Materialise a Dagster asset on demand.',                             FALSE),
    ('generate_report',                'R2', 'Run the report builder graph for a project + template.',             FALSE),
    ('create_export',                  'R4', 'Build a customer-shippable export bundle (PDF / DOCX / map pack).',  TRUE),
    ('request_approval',               'R3', 'Create an approval ticket for a downstream R4/R5 action.',           FALSE),
    ('publish_arcgis',                 'R4', 'Publish a layer pack to a customer ArcGIS endpoint.',                TRUE),
    ('query_public_geo',               'R0', 'Read public_geo.* layers.',                                   FALSE),
    ('create_review_item',             'R2', 'Add a row to silver.review_queue for SME triage.',                   FALSE),
    ('run_evaluation',                 'R1', 'Fire the eval harness against a question_set.',                      FALSE),
    ('create_target_recommendation',   'R2', 'Insert a row into targeting.target_recommendations.',                FALSE),
    ('record_decision',                'R2', 'Insert a row into silver.decision_records.',                          FALSE),
    ('record_field_outcome',           'R2', 'Insert into targeting.target_outcomes from a field report.',         FALSE)
ON CONFLICT (tool_name) DO UPDATE
    SET risk_tier        = EXCLUDED.risk_tier,
        description      = EXCLUDED.description,
        requires_dry_run = EXCLUDED.requires_dry_run;

-- =============================================================================
-- Default-permissive policy: every workspace gets ALLOW for every tool unless
-- explicitly denied. Customers can flip allowed=false per tool from the admin
-- agent-config UI (audit-flagged surface — exists, just needs to expose this
-- table). For now we don't auto-populate per-workspace rows; absence = ALLOW
-- by default in the gateway.
-- =============================================================================

-- Default approval requirements for R4 tools across all workspaces — these
-- ARE seeded eagerly so operators can change them later but the default is
-- safe (QP sign-off required).
DO $$
DECLARE
    ws_id uuid;
BEGIN
    -- For each existing workspace, ensure R4 tools have approval rows
    FOR ws_id IN SELECT workspace_id FROM silver.workspaces LOOP
        INSERT INTO workspace.approval_requirements
            (workspace_id, tool_name, required_role, min_credentials)
        VALUES
            (ws_id, 'create_export',   'qp_signoff', '{"qp_credential_verified": true}'::jsonb),
            (ws_id, 'publish_arcgis',  'qp_signoff', '{"qp_credential_verified": true}'::jsonb)
        ON CONFLICT (workspace_id, tool_name) DO NOTHING;
    END LOOP;
END $$;

-- =============================================================================
-- RLS — tenant isolation
-- =============================================================================
ALTER TABLE workspace.agent_permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.approval_requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.tool_invocations ENABLE ROW LEVEL SECURITY;
-- agent_risk_tiers is intentionally global (no RLS).

DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT unnest(ARRAY[
            'agent_permissions',
            'approval_requirements',
            'tool_invocations'
        ])
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS toolgw_ws_isolation ON workspace.%I', t);
        EXECUTE format($f$
            CREATE POLICY toolgw_ws_isolation ON workspace.%I
                USING (
                    workspace_id = (
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                    OR NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                )
                WITH CHECK (
                    workspace_id = (
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                )
        $f$, t);
    END LOOP;
END $$;

-- Grant table access
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
        GRANT SELECT
            ON workspace.agent_risk_tiers
            TO georag_app;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON workspace.agent_permissions,
               workspace.approval_requirements,
               workspace.tool_invocations
            TO georag_app;
    END IF;
END $$;

-- Verify
DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM workspace.agent_risk_tiers;
    RAISE NOTICE '§4 Tool Gateway: % tools registered', n;
END $$;
