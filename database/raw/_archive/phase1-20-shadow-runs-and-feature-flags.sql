-- =============================================================================
-- Phase 1 Step 4 supplement migration —
--   silver.shadow_runs        (per-shadow-run diff outcome)
--   workspace.feature_flags   (traffic-percent + per-workspace flags)
--
-- Both tables are referenced by the Phase 1 Step 5 shadow harness. We deploy
-- them in Step 4 so the Hatchet ingest_pdf workflow can emit shadow_run
-- correlation rows from its first end-to-end smoke.
--
-- Schema reference: docs/phase1_v149_ingest_pdf_survey.md §10.4 (diff
-- contract storage) + the ShadowRouter spec in the kickoff Step 5.
--
-- Idempotent. RLS-enabled per Phase 0 §95-rls-policies pattern.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- silver.shadow_runs
--
-- One row per (workspace_id, minio_key, run_started_at) tuple. The diff
-- worker (ai:shadow_diff Hatchet workflow, lands in Step 5) UPSERTs this
-- row when both v1.49 + Hatchet sides complete.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.shadow_runs (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id          uuid        NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    workflow_kind         text        NOT NULL,                   -- 'ingest_pdf' for Phase 1
    correlation_token     text        NOT NULL,                   -- shared between v1.49 + hatchet sides for this dual-write
    minio_key             text        NOT NULL,                   -- bronze object key (the input)
    classification        text        NOT NULL DEFAULT 'partial'
        CHECK (classification IN ('partial','clean','minor','divergent','fatal')),
    v149_result           jsonb       NULL,                       -- full v1.49 output (ReportParseResult equivalent)
    hatchet_result        jsonb       NULL,                       -- full Hatchet output
    diff_details          jsonb       NULL,                       -- per-field check outcomes (see survey §10.2)
    v149_duration_ms      integer     NULL,
    hatchet_duration_ms   integer     NULL,
    v149_audit_run_id     uuid        NULL,
    hatchet_audit_run_id  uuid        NULL,
    error_v149            text        NULL,                       -- if v1.49 raised
    error_hatchet         text        NULL,                       -- if Hatchet raised
    started_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at          timestamptz NULL,
    CONSTRAINT shadow_runs_correlation_unique UNIQUE (correlation_token)
);

COMMENT ON TABLE  silver.shadow_runs IS
    'Phase 1 shadow ingest comparison results. One row per dual-write; classification per docs/phase1_v149_ingest_pdf_survey.md §10.';
COMMENT ON COLUMN silver.shadow_runs.correlation_token IS
    'Shared opaque token written by Laravel-side ShadowRouter and observed by both paths so the diff worker can pair them.';
COMMENT ON COLUMN silver.shadow_runs.classification IS
    'partial = one side not yet complete; clean/minor/divergent/fatal per the diff contract.';

CREATE INDEX IF NOT EXISTS shadow_runs_workspace_started_idx
    ON silver.shadow_runs (workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS shadow_runs_classification_idx
    ON silver.shadow_runs (classification, started_at DESC);
CREATE INDEX IF NOT EXISTS shadow_runs_kind_started_idx
    ON silver.shadow_runs (workflow_kind, started_at DESC);

-- RLS: same tenant_isolation pattern as Phase 0
ALTER TABLE silver.shadow_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.shadow_runs FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON silver.shadow_runs;
CREATE POLICY tenant_isolation ON silver.shadow_runs
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
    );

GRANT SELECT, INSERT, UPDATE ON silver.shadow_runs TO georag_app;


-- ---------------------------------------------------------------------------
-- workspace.feature_flags
--
-- Generic typed key-value store for per-workspace feature toggles. A row's
-- workspace_id NULL means the flag is global / platform-wide.
--
-- Uses one column per type rather than a single jsonb so the most common
-- read path (boolean / int) doesn't pay the JSONB parse cost.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.feature_flags (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid        NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    flag_name           text        NOT NULL,
    bool_value          boolean     NULL,
    int_value           integer     NULL,
    string_value        text        NULL,
    json_value          jsonb       NULL,
    description         text        NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          bigint      NULL,
    -- NULLS NOT DISTINCT (PG18+) so the platform-default row
    -- (workspace_id IS NULL) is treated as a single key under
    -- ON CONFLICT — without this, the UPSERT path silently inserts
    -- duplicates because NULL <> NULL in standard SQL.
    CONSTRAINT feature_flags_unique UNIQUE NULLS NOT DISTINCT (workspace_id, flag_name),
    CONSTRAINT feature_flags_one_value CHECK (
        (CASE WHEN bool_value   IS NOT NULL THEN 1 ELSE 0 END +
         CASE WHEN int_value    IS NOT NULL THEN 1 ELSE 0 END +
         CASE WHEN string_value IS NOT NULL THEN 1 ELSE 0 END +
         CASE WHEN json_value   IS NOT NULL THEN 1 ELSE 0 END) >= 1
    )
);

COMMENT ON TABLE  workspace.feature_flags IS
    'Per-workspace feature toggles. workspace_id NULL = platform-wide default.';
COMMENT ON COLUMN workspace.feature_flags.flag_name IS
    'Canonical: ingest_pdf_hatchet_traffic_pct, ingest_pdf_shadow_enabled, etc.';

CREATE INDEX IF NOT EXISTS feature_flags_workspace_idx
    ON workspace.feature_flags (workspace_id, flag_name);

-- RLS
ALTER TABLE workspace.feature_flags ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.feature_flags FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON workspace.feature_flags;
CREATE POLICY tenant_isolation ON workspace.feature_flags
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

GRANT SELECT, INSERT, UPDATE, DELETE ON workspace.feature_flags TO georag_app;


-- ---------------------------------------------------------------------------
-- Platform-default seed for the ingest_pdf cutover. Starts at 0% — operator
-- bumps via the dashboard (Phase 1 Step 6) once shadow runs report clean.
-- ---------------------------------------------------------------------------
INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, bool_value, int_value, description)
VALUES
    (NULL, 'ingest_pdf_hatchet_traffic_pct', NULL, 0,
     'Phase 1 Step 5 — % of incoming ingest_pdf requests that go to BOTH the v1.49 path AND Hatchet (dual-write shadow). Ramp 0→1→10→50→100 over the 14-day window.'),
    (NULL, 'ingest_pdf_shadow_enabled', true, NULL,
     '(boolean) Master switch — disables shadow dual-write entirely if false.')
ON CONFLICT (workspace_id, flag_name) DO NOTHING;


-- ---------------------------------------------------------------------------
-- Forward-fix for installs that landed before NULLS NOT DISTINCT was set.
-- Idempotent — drops + recreates the constraint only if currently DISTINCT.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    nulls_distinct boolean;
BEGIN
    SELECT NOT i.indnullsnotdistinct INTO nulls_distinct
      FROM pg_constraint c
      JOIN pg_index i ON i.indexrelid = c.conindid
     WHERE c.conrelid = 'workspace.feature_flags'::regclass
       AND c.conname  = 'feature_flags_unique';
    IF nulls_distinct IS TRUE THEN
        -- De-dupe any rows that slipped past the broken constraint.
        DELETE FROM workspace.feature_flags fa
         WHERE EXISTS (
            SELECT 1 FROM workspace.feature_flags fb
             WHERE fb.workspace_id IS NULL
               AND fb.workspace_id IS NOT DISTINCT FROM fa.workspace_id
               AND fb.flag_name    = fa.flag_name
               AND fb.created_at   < fa.created_at
         );
        ALTER TABLE workspace.feature_flags
            DROP CONSTRAINT feature_flags_unique;
        ALTER TABLE workspace.feature_flags
            ADD  CONSTRAINT feature_flags_unique
                 UNIQUE NULLS NOT DISTINCT (workspace_id, flag_name);
        RAISE NOTICE 'feature_flags_unique upgraded to NULLS NOT DISTINCT';
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n_shadow int;
    n_flags int;
BEGIN
    SELECT count(*) INTO n_shadow FROM information_schema.tables
        WHERE table_schema='silver' AND table_name='shadow_runs';
    SELECT count(*) INTO n_flags FROM information_schema.tables
        WHERE table_schema='workspace' AND table_name='feature_flags';
    RAISE NOTICE 'Phase 1 step 4 schema: silver.shadow_runs=%, workspace.feature_flags=%',
        n_shadow, n_flags;
END $$;
