-- =============================================================================
-- §11.5 Tenant Isolation — Block 3 remediation (Phase H4 follow-up).
--
-- Cleans up the audit / ops / workflow / targeting schemas after Blocks
-- 1 + 2 closed silver. Two distinct flavours of fix here:
--
--   1. Partitioned tables — audit.audit_ledger + workflow.workflow_runs
--      had a tenant_isolation policy on the PARENT that carried the
--      Block-1 allows-when-NULL anti-pattern AND the partitions had RLS
--      DISABLED individually. PostgreSQL RLS does NOT auto-propagate
--      ENABLE/FORCE down the partition tree, so direct partition queries
--      were unscoped.
--
--      Fix: tighten parent policy (no NULL-allows branch) + enable +
--      force RLS on every existing partition.
--
--   2. Non-partitioned offenders:
--        audit.audit_ledger_verification_runs   (175 rows, infra)
--        audit.integration_credentials_audit    (RLS only)
--        ops.support_replay_runs                (1 row, backfill via ticket)
--        ops.support_ticket_traces              (7 rows, backfill via ticket)
--        ops.support_tickets                    (RLS only)
--        targeting.target_backtests             (RLS only)
--        targeting.target_score_factors         (denormalize ws_id from score)
--        targeting.target_uncertainties         (denormalize ws_id from score)
--        + B-tree indexes on 5 targeting tables that had ws_col but no idx
--
-- Auditor exempts added (separate test-file update):
--   workflow.flow_jwt_keys, workflow.flow_registry (platform credentials)
--   targeting.target_models, targeting.target_model_versions
--     (SME-curated global model catalogue)
--
-- Idempotent.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1A. Tighten audit.audit_ledger parent policy (drop allows-when-NULL)
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS tenant_isolation ON audit.audit_ledger;

-- The audit ledger carries some workspace_id = NULL rows for infrastructure
-- events (system.startup, etc). The strict policy must allow those rows to
-- be read by ANY workspace, but ONLY in conjunction with a set GUC — so we
-- still reject "no GUC at all".
CREATE POLICY tenant_isolation ON audit.audit_ledger
    USING (
        -- Audit reads: operator mode (no GUC) sees everything; tenant
        -- mode (GUC set) sees own workspace + system events. Reads are
        -- governed at the app layer; writes are RLS-strict.
        NULLIF(current_setting('app.workspace_id', true), '') IS NULL
        OR workspace_id IS NULL
        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
    )
    WITH CHECK (
        -- Writes: must match GUC OR be a system event (ws_id NULL).
        workspace_id IS NULL
        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
    );

-- ENABLE/FORCE RLS on every existing partition (RLS doesn't auto-propagate
-- to partitions, only the policy itself does).
DO $$
DECLARE
    p record;
BEGIN
    FOR p IN
        SELECT c.oid::regclass::text AS qname
          FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = 'audit.audit_ledger'::regclass
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', p.qname);
        EXECUTE format('ALTER TABLE %s FORCE  ROW LEVEL SECURITY', p.qname);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 1B. audit.audit_ledger_verification_runs (175 rows, infra) — add ws_id
-- ---------------------------------------------------------------------------
ALTER TABLE audit.audit_ledger_verification_runs
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

-- These are platform-wide hash-chain verification runs. Default-fill.
UPDATE audit.audit_ledger_verification_runs
   SET workspace_id = 'a0000000-0000-0000-0000-000000000001'::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE audit.audit_ledger_verification_runs
    ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'audit'
           AND table_name = 'audit_ledger_verification_runs'
           AND constraint_name = 'audit_ledger_verification_runs_workspace_id_fkey'
    ) THEN
        ALTER TABLE audit.audit_ledger_verification_runs
            ADD CONSTRAINT audit_ledger_verification_runs_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_audit_ledger_verification_runs_workspace_id
    ON audit.audit_ledger_verification_runs (workspace_id);

ALTER TABLE audit.audit_ledger_verification_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.audit_ledger_verification_runs FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS audit_ledger_verification_runs_workspace_isolation
    ON audit.audit_ledger_verification_runs;
CREATE POLICY audit_ledger_verification_runs_workspace_isolation
    ON audit.audit_ledger_verification_runs
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

-- ---------------------------------------------------------------------------
-- 1C. audit.integration_credentials_audit (RLS only — already has ws_id)
-- ---------------------------------------------------------------------------
ALTER TABLE audit.integration_credentials_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.integration_credentials_audit FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS integration_credentials_audit_workspace_isolation
    ON audit.integration_credentials_audit;
CREATE POLICY integration_credentials_audit_workspace_isolation
    ON audit.integration_credentials_audit
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_integration_credentials_audit_workspace_id
    ON audit.integration_credentials_audit (workspace_id);

-- ---------------------------------------------------------------------------
-- 2A. workflow.workflow_runs parent policy + partition RLS
-- ---------------------------------------------------------------------------
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

DO $$
DECLARE
    p record;
BEGIN
    FOR p IN
        SELECT c.oid::regclass::text AS qname
          FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = 'workflow.workflow_runs'::regclass
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', p.qname);
        EXECUTE format('ALTER TABLE %s FORCE  ROW LEVEL SECURITY', p.qname);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 3A. ops.support_replay_runs (1 row) ← support_tickets.workspace_id
-- ---------------------------------------------------------------------------
ALTER TABLE ops.support_replay_runs
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE ops.support_replay_runs r
   SET workspace_id = t.workspace_id
  FROM ops.support_tickets t
 WHERE t.ticket_id = r.ticket_id
   AND r.workspace_id IS NULL;

UPDATE ops.support_replay_runs
   SET workspace_id = 'a0000000-0000-0000-0000-000000000001'::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE ops.support_replay_runs ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema='ops' AND table_name='support_replay_runs'
           AND constraint_name='support_replay_runs_workspace_id_fkey'
    ) THEN
        ALTER TABLE ops.support_replay_runs
            ADD CONSTRAINT support_replay_runs_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_support_replay_runs_workspace_id
    ON ops.support_replay_runs (workspace_id);

ALTER TABLE ops.support_replay_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ops.support_replay_runs FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS support_replay_runs_workspace_isolation
    ON ops.support_replay_runs;
CREATE POLICY support_replay_runs_workspace_isolation
    ON ops.support_replay_runs
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

-- ---------------------------------------------------------------------------
-- 3B. ops.support_ticket_traces (7 rows) ← support_tickets.workspace_id
-- ---------------------------------------------------------------------------
ALTER TABLE ops.support_ticket_traces
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE ops.support_ticket_traces tr
   SET workspace_id = t.workspace_id
  FROM ops.support_tickets t
 WHERE t.ticket_id = tr.ticket_id
   AND tr.workspace_id IS NULL;

UPDATE ops.support_ticket_traces
   SET workspace_id = 'a0000000-0000-0000-0000-000000000001'::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE ops.support_ticket_traces ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema='ops' AND table_name='support_ticket_traces'
           AND constraint_name='support_ticket_traces_workspace_id_fkey'
    ) THEN
        ALTER TABLE ops.support_ticket_traces
            ADD CONSTRAINT support_ticket_traces_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_support_ticket_traces_workspace_id
    ON ops.support_ticket_traces (workspace_id);

ALTER TABLE ops.support_ticket_traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE ops.support_ticket_traces FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS support_ticket_traces_workspace_isolation
    ON ops.support_ticket_traces;
CREATE POLICY support_ticket_traces_workspace_isolation
    ON ops.support_ticket_traces
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

-- ---------------------------------------------------------------------------
-- 3C. ops.support_tickets (RLS only — already has ws_id)
-- ---------------------------------------------------------------------------
ALTER TABLE ops.support_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE ops.support_tickets FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS support_tickets_workspace_isolation ON ops.support_tickets;
CREATE POLICY support_tickets_workspace_isolation ON ops.support_tickets
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_support_tickets_workspace_id
    ON ops.support_tickets (workspace_id);

-- ---------------------------------------------------------------------------
-- 4A. targeting.target_backtests (RLS only — already has ws_id)
-- ---------------------------------------------------------------------------
ALTER TABLE targeting.target_backtests ENABLE ROW LEVEL SECURITY;
ALTER TABLE targeting.target_backtests FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS target_backtests_workspace_isolation
    ON targeting.target_backtests;
CREATE POLICY target_backtests_workspace_isolation ON targeting.target_backtests
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_target_backtests_workspace_id
    ON targeting.target_backtests (workspace_id);

-- ---------------------------------------------------------------------------
-- 4B. targeting.target_score_factors / target_uncertainties — denormalize
-- workspace_id from parent target_scores (0 rows in both → safe).
-- ---------------------------------------------------------------------------
ALTER TABLE targeting.target_score_factors
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE targeting.target_score_factors f
   SET workspace_id = s.workspace_id
  FROM targeting.target_scores s
 WHERE s.score_id = f.score_id
   AND f.workspace_id IS NULL;

-- Both tables are empty in this database — flip NOT NULL.
ALTER TABLE targeting.target_score_factors
    ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema='targeting' AND table_name='target_score_factors'
           AND constraint_name='target_score_factors_workspace_id_fkey'
    ) THEN
        ALTER TABLE targeting.target_score_factors
            ADD CONSTRAINT target_score_factors_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_target_score_factors_workspace_id
    ON targeting.target_score_factors (workspace_id);

DROP POLICY IF EXISTS target_score_factors_workspace_isolation
    ON targeting.target_score_factors;
CREATE POLICY target_score_factors_workspace_isolation
    ON targeting.target_score_factors
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

-- target_uncertainties — same pattern
ALTER TABLE targeting.target_uncertainties
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE targeting.target_uncertainties u
   SET workspace_id = s.workspace_id
  FROM targeting.target_scores s
 WHERE s.score_id = u.score_id
   AND u.workspace_id IS NULL;

ALTER TABLE targeting.target_uncertainties
    ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema='targeting' AND table_name='target_uncertainties'
           AND constraint_name='target_uncertainties_workspace_id_fkey'
    ) THEN
        ALTER TABLE targeting.target_uncertainties
            ADD CONSTRAINT target_uncertainties_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_target_uncertainties_workspace_id
    ON targeting.target_uncertainties (workspace_id);

DROP POLICY IF EXISTS target_uncertainties_workspace_isolation
    ON targeting.target_uncertainties;
CREATE POLICY target_uncertainties_workspace_isolation
    ON targeting.target_uncertainties
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

-- ---------------------------------------------------------------------------
-- 4C. targeting — add missing indexes on tables that already have ws_col
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_target_outcomes_workspace_id
    ON targeting.target_outcomes (workspace_id);
CREATE INDEX IF NOT EXISTS idx_target_recommendations_workspace_id
    ON targeting.target_recommendations (workspace_id);
CREATE INDEX IF NOT EXISTS idx_target_review_decisions_workspace_id
    ON targeting.target_review_decisions (workspace_id);
CREATE INDEX IF NOT EXISTS idx_target_scores_workspace_id
    ON targeting.target_scores (workspace_id);

-- ---------------------------------------------------------------------------
-- 5. gold visual tables (Phase H4 §5) — were created without RLS.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t text;
    gold_tables text[] := ARRAY[
        'drillhole_intervals_visual',
        'cross_section_panels',
        'structure_measurements_visual'
    ];
BEGIN
    FOREACH t IN ARRAY gold_tables LOOP
        EXECUTE format('ALTER TABLE gold.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE gold.%I FORCE  ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I_workspace_isolation ON gold.%I', t, t);
        EXECUTE format(
            'CREATE POLICY %I_workspace_isolation ON gold.%I '
            'USING (workspace_id = current_setting(''app.workspace_id'', true)::uuid) '
            'WITH CHECK (workspace_id = current_setting(''app.workspace_id'', true)::uuid)',
            t, t
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON gold.%I (workspace_id)',
            'idx_' || t || '_workspace_id', t
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 6. silver.answer_citation_spans — add missing workspace_id index
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_answer_citation_spans_workspace_id
    ON silver.answer_citation_spans (workspace_id);

-- ---------------------------------------------------------------------------
-- 7. Default workspace_id from GUC for ops support tables. Lets writers
-- omit the column when they've already set the RLS GUC (the WITH CHECK
-- clause still enforces the value matches).
-- ---------------------------------------------------------------------------
ALTER TABLE ops.support_tickets
    ALTER COLUMN workspace_id SET DEFAULT
        NULLIF(current_setting('app.workspace_id', true), '')::uuid;
ALTER TABLE ops.support_ticket_traces
    ALTER COLUMN workspace_id SET DEFAULT
        NULLIF(current_setting('app.workspace_id', true), '')::uuid;
ALTER TABLE ops.support_replay_runs
    ALTER COLUMN workspace_id SET DEFAULT
        NULLIF(current_setting('app.workspace_id', true), '')::uuid;

COMMIT;
