-- =============================================================================
-- §11.5 Tenant Isolation — Block 2 remediation (Phase H4 follow-up).
--
-- Continues from Block 1 (96-rls-tenant-isolation-block1.sql) by sweeping
-- the rest of the silver schema. Every silver table that wasn't a Block 1
-- target gets one of three treatments:
--
--   Tier B — table already carries workspace_id but lacks a strict RLS
--            policy and/or index. Add policy + index, enable RLS.
--
--   Tier C — empty table missing workspace_id entirely. Add the column
--            (NOT NULL after default-fill via a no-op since the table
--            is empty), FK CASCADE to silver.workspaces, B-tree index,
--            enable RLS, add policy.
--
--   Tier D — small (≤ 10 rows) table needing backfill through a parent
--            table that already carries workspace_id. Backfill via
--            JOIN before flipping NOT NULL.
--
-- Shared reference data NOT scoped here (added to auditor exempt list):
--   silver.geological_ontology_terms, silver.geological_ontology_synonyms
--
-- Strict policy form (applied uniformly):
--   USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
--   WITH CHECK (...same...)
--
-- Idempotent. Re-run-safe.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Tier B — tables that have workspace_id but lack RLS/policy/index.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t text;
    tier_b_tables text[] := ARRAY[
        'projects',
        'kg_formation_aliases', 'kg_mineral_aliases',
        'kg_report_aliases',    'kg_sample_aliases',
        'geological_formations', 'historic_workings', 'project_boundaries',
        'collaboration_audit_log',  'collaboration_comments',
        'collaboration_mentions',   'collaboration_review_requests',
        'drill_traces', 'review_queue'
    ];
BEGIN
    FOREACH t IN ARRAY tier_b_tables LOOP
        -- Enable + force RLS
        EXECUTE format('ALTER TABLE silver.%I ENABLE  ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE silver.%I FORCE   ROW LEVEL SECURITY', t);

        -- Drop any prior workspace_id policy under either of the conventional names
        EXECUTE format('DROP POLICY IF EXISTS %I_workspace_isolation ON silver.%I', t, t);
        EXECUTE format('DROP POLICY IF EXISTS %I_project_scope        ON silver.%I', t, t);
        EXECUTE format('DROP POLICY IF EXISTS %I_owner_access          ON silver.%I', t, t);

        -- Strict workspace_id policy
        EXECUTE format(
            'CREATE POLICY %I_workspace_isolation ON silver.%I '
            'USING (workspace_id = current_setting(''app.workspace_id'', true)::uuid) '
            'WITH CHECK (workspace_id = current_setting(''app.workspace_id'', true)::uuid)',
            t, t
        );

        -- B-tree index on workspace_id (idempotent)
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON silver.%I (workspace_id)',
            'idx_' || t || '_workspace_id', t
        );
    END LOOP;
END $$;

-- silver.projects also needs to flip its workspace_id from nullable to NOT NULL.
UPDATE silver.projects SET workspace_id = 'a0000000-0000-0000-0000-000000000001'::uuid
 WHERE workspace_id IS NULL;
ALTER TABLE silver.projects ALTER COLUMN workspace_id SET NOT NULL;

-- ---------------------------------------------------------------------------
-- Tier C — empty silver tables missing workspace_id entirely.
-- Add the column + FK + index + RLS + policy in one swoop.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t text;
    tier_c_tables text[] := ARRAY[
        'alterations', 'structures', 'surveys',
        'decision_evidence_links', 'decision_lessons_learned',
        'decision_outcomes',
        'agent_conversation_messages', 'agent_conversations',
        'exports',
        'pdf_coordinates',     'pdf_layout_regions',
        'pdf_ocr_results',     'pdf_table_cells',
        'pdf_text_blocks',     'pdf_vl_summaries',
        'raster_layers',       'seismic_surveys',
        'structured_record_lineage',
        'source_trust_features',
        'mineral_claims',      'review_audit_log'
    ];
    has_col boolean;
    has_fk  boolean;
BEGIN
    FOREACH t IN ARRAY tier_c_tables LOOP
        -- 1. Add column if missing
        EXECUTE format('SELECT 1 FROM information_schema.columns '
                       'WHERE table_schema = ''silver'' AND table_name = %L '
                       'AND column_name = ''workspace_id''', t)
            INTO has_col;
        IF has_col IS NULL THEN
            EXECUTE format('ALTER TABLE silver.%I ADD COLUMN workspace_id uuid', t);
        END IF;

        -- 2. Empty table → safe to enforce NOT NULL immediately
        EXECUTE format(
            'ALTER TABLE silver.%I ALTER COLUMN workspace_id SET NOT NULL', t
        );

        -- 3. FK CASCADE
        EXECUTE format(
            'SELECT 1 FROM information_schema.table_constraints '
            'WHERE table_schema = ''silver'' AND table_name = %L '
            'AND constraint_name = %L', t, t || '_workspace_id_fkey'
        ) INTO has_fk;
        IF has_fk IS NULL THEN
            EXECUTE format(
                'ALTER TABLE silver.%I ADD CONSTRAINT %I '
                'FOREIGN KEY (workspace_id) '
                'REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE',
                t, t || '_workspace_id_fkey'
            );
        END IF;

        -- 4. Index
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON silver.%I (workspace_id)',
            'idx_' || t || '_workspace_id', t
        );

        -- 5. RLS + policy
        EXECUTE format('ALTER TABLE silver.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE silver.%I FORCE  ROW LEVEL SECURITY', t);

        EXECUTE format('DROP POLICY IF EXISTS %I_workspace_isolation ON silver.%I', t, t);
        EXECUTE format(
            'CREATE POLICY %I_workspace_isolation ON silver.%I '
            'USING (workspace_id = current_setting(''app.workspace_id'', true)::uuid) '
            'WITH CHECK (workspace_id = current_setting(''app.workspace_id'', true)::uuid)',
            t, t
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Tier D — small tables with backfill via parent
-- ---------------------------------------------------------------------------

-- silver.decision_options (4 rows) ← silver.decision_records.workspace_id
ALTER TABLE silver.decision_options
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE silver.decision_options d
   SET workspace_id = r.workspace_id
  FROM silver.decision_records r
 WHERE r.decision_id = d.decision_id
   AND d.workspace_id IS NULL;

UPDATE silver.decision_options
   SET workspace_id = 'a0000000-0000-0000-0000-000000000001'::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE silver.decision_options ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'silver' AND table_name = 'decision_options'
           AND constraint_name = 'decision_options_workspace_id_fkey'
    ) THEN
        ALTER TABLE silver.decision_options
            ADD CONSTRAINT decision_options_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_decision_options_workspace_id
    ON silver.decision_options (workspace_id);

ALTER TABLE silver.decision_options ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.decision_options FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS decision_options_workspace_isolation ON silver.decision_options;
CREATE POLICY decision_options_workspace_isolation ON silver.decision_options
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

-- silver.lithology_logs (4 rows) ← silver.collars.workspace_id
ALTER TABLE silver.lithology_logs
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE silver.lithology_logs l
   SET workspace_id = c.workspace_id
  FROM silver.collars c
 WHERE c.collar_id = l.collar_id
   AND l.workspace_id IS NULL;

UPDATE silver.lithology_logs
   SET workspace_id = 'a0000000-0000-0000-0000-000000000001'::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE silver.lithology_logs ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'silver' AND table_name = 'lithology_logs'
           AND constraint_name = 'lithology_logs_workspace_id_fkey'
    ) THEN
        ALTER TABLE silver.lithology_logs
            ADD CONSTRAINT lithology_logs_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_lithology_logs_workspace_id
    ON silver.lithology_logs (workspace_id);

ALTER TABLE silver.lithology_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.lithology_logs FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lithology_logs_workspace_isolation ON silver.lithology_logs;
CREATE POLICY lithology_logs_workspace_isolation ON silver.lithology_logs
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

COMMIT;
