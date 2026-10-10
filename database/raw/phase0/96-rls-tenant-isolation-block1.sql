-- =============================================================================
-- §11.5 Tenant Isolation — Block 1 remediation (Phase H4 follow-up).
--
-- Fixes the cross-tenant leak primitive caught by the Tenant Isolation
-- Auditor on 2026-05-15:
--
--   silver.collars (and several siblings) carried RLS policies of the
--   form `current_setting('georag.project_id', true) IS NULL OR ...`.
--   The NULL branch is allows-everything — any client that fails to
--   set the GUC saw every workspace's rows.
--
-- This migration covers the top-5 highest-traffic silver tables that
-- were missing workspace_id + had broken RLS:
--
--   1. silver.collars                  (83 rows)
--   2. silver.reports                  (1,165 rows; 57 orphans → Default Workspace)
--   3. silver.well_log_curves          (753 rows; RLS was OFF)
--   4. silver.hypothesis_evidence_links (27 rows; policy allowed NULL)
--   5. silver.spatial_features         (0 rows; RLS was OFF)
--
-- Backfill convention:
--   * Rows with project_id → workspace_id resolved from silver.projects.
--   * Orphan reports (project_id NULL or unresolved) → Default Workspace
--     (a0000000-0000-0000-0000-000000000001). Operators can re-scope
--     via the UI; this preserves them rather than dropping them.
--
-- Idempotent: ALTERs / CREATEs guarded by IF NOT EXISTS / IF EXISTS.
--
-- AS BUILT (database audit 2026-10) -- read before relying on "strict" below.
-- On a cluster built the way the ECS migrate task builds one (`migrate`, THEN
-- `db:apply-raw`) the policies this file creates are NOT the only policies on
-- three of its tables, and the table is therefore NOT strict:
--
--   silver.collars          collars_workspace_isolation_v2
--   silver.reports          silver_reports_workspace_isolation_v2
--   silver.spatial_features silver_spatial_features_workspace_isolation_v2
--
-- Each `_v2` policy is fail-open (`<GUC unset> OR workspace_id = <GUC>`). collars'
-- comes from 2026_05_25_184630, reports' and spatial_features' from
-- 2026_05_25_175214; both migrations install it "only when nothing covers the
-- table yet", i.e. they assume this file ran FIRST. The ECS migrate task runs the
-- migrations first, so the sibling lands, this file DROPs only the policy NAMES it
-- knows about, and permissive policies are OR-ed: the strict policy below is
-- decided by its fail-open sibling. Net effect, measured on a migrate + raw
-- database: a session with a workspace BOUND is clamped to it; a session with NO
-- workspace bound reads and writes every tenant's rows. (silver.well_log_curves
-- and silver.hypothesis_evidence_links are the two tables here that really are
-- strict; they have no sibling.)
--
-- Left like this on purpose, pending a decision: workflow.refresh_silver_agent_mvs()
-- refreshes the cross-tenant silver.mv_collar_summary as the table owner, which on
-- RDS is not a superuser and so is bound by these policies; with only the strict
-- policy the refreshed view is empty. See docs/architecture/manual/11-tenancy-and-rls.md
-- section 4.1 for the full list of affected tables and what making them strict takes.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Constants
-- ---------------------------------------------------------------------------
\set default_workspace_id '\'a0000000-0000-0000-0000-000000000001\''

-- ---------------------------------------------------------------------------
-- 1. silver.collars  ── add workspace_id, backfill, FK, idx, strict RLS
-- ---------------------------------------------------------------------------
ALTER TABLE silver.collars
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE silver.collars c
   SET workspace_id = p.workspace_id
  FROM silver.projects p
 WHERE p.project_id = c.project_id
   AND c.workspace_id IS NULL;

-- Any residual NULLs (no resolvable project) go to Default Workspace.
UPDATE silver.collars
   SET workspace_id = :default_workspace_id::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE silver.collars
    ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'silver'
           AND table_name   = 'collars'
           AND constraint_name = 'collars_workspace_id_fkey'
    ) THEN
        ALTER TABLE silver.collars
            ADD CONSTRAINT collars_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_collars_workspace_id
    ON silver.collars (workspace_id);

-- Drop the legacy project-scoped policy that allowed-when-NULL, and the
-- workspace_isolation policy itself so a re-run (db:apply-raw runs every
-- deploy, unconditionally) doesn't fail with "policy already exists" —
-- verified live on a go-live rehearsal (2026-09-18): the DROP below was
-- missing and a second run of this file failed exactly that way.
DROP POLICY IF EXISTS collars_project_scope    ON silver.collars;
DROP POLICY IF EXISTS collars_owner_access     ON silver.collars;
DROP POLICY IF EXISTS collars_workspace_isolation ON silver.collars;

ALTER TABLE silver.collars ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.collars FORCE  ROW LEVEL SECURITY;

CREATE POLICY collars_workspace_isolation ON silver.collars
    USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- 2. silver.reports  ── add workspace_id, backfill (orphans → default), strict RLS
-- ---------------------------------------------------------------------------
ALTER TABLE silver.reports
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE silver.reports r
   SET workspace_id = p.workspace_id
  FROM silver.projects p
 WHERE p.project_id = r.project_id
   AND r.workspace_id IS NULL;

-- Orphan reports (no project_id or unresolved) → Default Workspace.
UPDATE silver.reports
   SET workspace_id = :default_workspace_id::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE silver.reports
    ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'silver'
           AND table_name   = 'reports'
           AND constraint_name = 'reports_workspace_id_fkey'
    ) THEN
        ALTER TABLE silver.reports
            ADD CONSTRAINT reports_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_reports_workspace_id
    ON silver.reports (workspace_id);

DROP POLICY IF EXISTS reports_project_scope    ON silver.reports;
DROP POLICY IF EXISTS reports_owner_access     ON silver.reports;
DROP POLICY IF EXISTS reports_workspace_isolation ON silver.reports;

ALTER TABLE silver.reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.reports FORCE  ROW LEVEL SECURITY;

CREATE POLICY reports_workspace_isolation ON silver.reports
    USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- 3. silver.well_log_curves  ── workspace_id via collar → project, strict RLS
-- ---------------------------------------------------------------------------
ALTER TABLE silver.well_log_curves
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE silver.well_log_curves w
   SET workspace_id = c.workspace_id
  FROM silver.collars c
 WHERE c.collar_id = w.collar_id
   AND w.workspace_id IS NULL;

UPDATE silver.well_log_curves
   SET workspace_id = :default_workspace_id::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE silver.well_log_curves
    ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'silver'
           AND table_name   = 'well_log_curves'
           AND constraint_name = 'well_log_curves_workspace_id_fkey'
    ) THEN
        ALTER TABLE silver.well_log_curves
            ADD CONSTRAINT well_log_curves_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_well_log_curves_workspace_id
    ON silver.well_log_curves (workspace_id);

ALTER TABLE silver.well_log_curves ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.well_log_curves FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS well_log_curves_project_scope     ON silver.well_log_curves;
DROP POLICY IF EXISTS well_log_curves_workspace_isolation ON silver.well_log_curves;

CREATE POLICY well_log_curves_workspace_isolation ON silver.well_log_curves
    USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- 4. silver.hypothesis_evidence_links  ── add direct workspace_id + strict RLS
--    (existing policy used a join to hypotheses with NULL-allows fallback)
-- ---------------------------------------------------------------------------
ALTER TABLE silver.hypothesis_evidence_links
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

UPDATE silver.hypothesis_evidence_links l
   SET workspace_id = h.workspace_id
  FROM silver.hypotheses h
 WHERE h.hypothesis_id = l.hypothesis_id
   AND l.workspace_id IS NULL;

UPDATE silver.hypothesis_evidence_links
   SET workspace_id = :default_workspace_id::uuid
 WHERE workspace_id IS NULL;

ALTER TABLE silver.hypothesis_evidence_links
    ALTER COLUMN workspace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'silver'
           AND table_name   = 'hypothesis_evidence_links'
           AND constraint_name = 'hypothesis_evidence_links_workspace_id_fkey'
    ) THEN
        ALTER TABLE silver.hypothesis_evidence_links
            ADD CONSTRAINT hypothesis_evidence_links_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_hypothesis_evidence_links_workspace_id
    ON silver.hypothesis_evidence_links (workspace_id);

DROP POLICY IF EXISTS hypothesis_evidence_links_workspace_isolation
    ON silver.hypothesis_evidence_links;

ALTER TABLE silver.hypothesis_evidence_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.hypothesis_evidence_links FORCE  ROW LEVEL SECURITY;

CREATE POLICY hypothesis_evidence_links_workspace_isolation
    ON silver.hypothesis_evidence_links
    USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- 5. silver.spatial_features  ── add workspace_id, enable RLS (table empty)
-- ---------------------------------------------------------------------------
ALTER TABLE silver.spatial_features
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

-- Empty table → no backfill needed. Mark NOT NULL after future inserts
-- carry the column. For now allow nullable so existing seeders that
-- assume the prior schema still work; the column will tighten in
-- Block 2.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'silver'
           AND table_name   = 'spatial_features'
           AND constraint_name = 'spatial_features_workspace_id_fkey'
    ) THEN
        ALTER TABLE silver.spatial_features
            ADD CONSTRAINT spatial_features_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_spatial_features_workspace_id
    ON silver.spatial_features (workspace_id);

ALTER TABLE silver.spatial_features ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.spatial_features FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS spatial_features_workspace_isolation ON silver.spatial_features;

CREATE POLICY spatial_features_workspace_isolation ON silver.spatial_features
    USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

COMMIT;

-- =============================================================================
-- Verification (run after COMMIT in psql to confirm the auditor flips green):
--
--   SET app.workspace_id = '11111111-1111-1111-1111-111111111111';
--   SELECT count(*) FROM silver.collars;        -- expect 0
--   SET app.workspace_id = 'a0000000-0000-0000-0000-000000000001';
--   SELECT count(*) FROM silver.collars;        -- expect 83
-- =============================================================================
