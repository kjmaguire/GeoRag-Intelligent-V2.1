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
        -- kg_formation_aliases/kg_mineral_aliases/kg_report_aliases/
        -- kg_sample_aliases deliberately absent: Neo4j and its knowledge
        -- graph were removed 2026-07-28 (CLAUDE.md hard rule 9) and no
        -- migration or raw SQL file has ever created these tables --
        -- verified via full git history search on a go-live rehearsal
        -- (2026-09-18). They were dangling references from before that
        -- removal; ALTER TABLE on a nonexistent relation fails loudly
        -- ("relation does not exist"), which is what a real deploy hit.
        'geological_formations', 'historic_workings', 'project_boundaries',
        -- collaboration_audit_log/collaboration_comments/collaboration_
        -- mentions/collaboration_review_requests deliberately absent, same
        -- reason as the kg_* entries above: verified live on the same
        -- go-live rehearsal (2026-09-18) that this ARRAY reached
        -- collaboration_audit_log next and failed identically ("relation
        -- does not exist"). No table under any of these four names has
        -- ever existed. The real collaboration tables are silver.
        -- collab_anchors and silver.collab_comments (created by
        -- 2026_05_16_120200_create_collab_anchors_and_comments.php) and
        -- both already have workspace RLS from
        -- 2026_05_19_180100_enable_rls_on_uncovered_workspace_tables.php —
        -- this array's four entries were never that migration's tables
        -- under a different name, just dead references.
        'drill_traces', 'review_queue'
    ];
BEGIN
    FOREACH t IN ARRAY tier_b_tables LOOP
        -- Defensive existence guard, not a substitute for the removals
        -- above. A skip here is a coverage gap to chase down, not a clean
        -- bill of health: every NOTICE it raises should end in either a
        -- removal from the array or a missing CREATE TABLE being added.
        IF to_regclass('silver.' || t) IS NULL THEN
            RAISE NOTICE 'Tier B: silver.% does not exist -- skipping RLS for it, not a known-dead reference', t;
            CONTINUE;
        END IF;

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
        -- alterations/structures deliberately absent: the 2026-09-18
        -- go-live rehearsal saw silver.alterations exist on one CD attempt
        -- and vanish on the next. Not data loss -- migration
        -- 2026_05_20_060400_create_silver_geological_singulars.php drops
        -- both empty plurals by design and replaces them with the singular
        -- spec tables silver.alteration / silver.structure, which get
        -- their RLS from 2026_05_20_060800_enable_rls_on_drillhole_tables.
        -- The first attempt had failed before reaching 060400.
        'surveys',
        'decision_evidence_links', 'decision_lessons_learned',
        'decision_outcomes',
        -- agent_conversation_messages/agent_conversations/pdf_coordinates/
        -- pdf_layout_regions/pdf_ocr_results/pdf_table_cells/pdf_text_blocks/
        -- mineral_claims deliberately absent: verified live on a go-live
        -- rehearsal (2026-09-18), same reason as the Tier B removals above
        -- -- no CREATE TABLE for any of them anywhere in this repository,
        -- not even in database/raw/_archive/. mineral_claims exists ONLY as
        -- a SQLite mirror for tests (2026_06_29_020000_provision_project_
        -- delete_tables_for_test_db.php, sqlite-only), never as a real
        -- Postgres table. pdf_vl_summaries IS real (created elsewhere) and
        -- stays.
        'exports',
        'pdf_vl_summaries',
        'raster_layers',       'seismic_surveys',
        'structured_record_lineage',
        'source_trust_features',
        'review_audit_log'
    ];
    has_col boolean;
    has_fk  boolean;
BEGIN
    FOREACH t IN ARRAY tier_c_tables LOOP
        -- Same defensive guard as Tier B above -- see that comment.
        IF to_regclass('silver.' || t) IS NULL THEN
            RAISE NOTICE 'Tier C: silver.% does not exist -- skipping RLS for it, not a known-dead reference', t;
            CONTINUE;
        END IF;

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
