<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Provision silver.drill_traces in the Laravel test DB.
 *
 * ADR-0007 PR-4 — sibling to
 * 2026_04_20_170000_create_silver_drill_traces.php and
 * 2026_05_30_010000_enable_rls_silver_drill_traces.php.
 *
 * Convention (project_test_db_parity_gap.md): for every raw-SQL table that
 * production gets from a phase-0 import, add a *_provision_*_for_test_db.php
 * sibling that creates the same table structure so downstream Laravel
 * migrations that ALTER or reference the table don't fail on a fresh test DB.
 *
 * silver.drill_traces already ships as a real Laravel migration so this
 * provision is a thin CREATE TABLE IF NOT EXISTS guard that ensures the
 * RLS migration above has a target table to operate on in isolated test
 * environments where the schema may not have been migrated in full.
 *
 * `CREATE TABLE IF NOT EXISTS` is a no-op on production where the original
 * migration ran first; on the test DB it creates the table with the correct
 * shape. `ENABLE ROW LEVEL SECURITY` is also applied here so the test DB
 * matches the production RLS coverage checked by WorkspaceRlsCoverageTest.
 *
 * PostGIS `geometry` type requires the extension to be enabled. The test DB
 * runs PostgreSQL + PostGIS, so no guard is needed.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.drill_traces (
                trace_id            UUID        NOT NULL DEFAULT gen_random_uuid(),
                collar_id           UUID        NOT NULL,
                workspace_id        UUID        NOT NULL,
                project_id          UUID        NOT NULL,
                geom                GEOMETRY(LINESTRINGZ, 4326) NOT NULL,
                computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                survey_hash         CHAR(64)    NOT NULL,
                dogleg_max_deg      NUMERIC(6,3) NULL,
                trace_quality       VARCHAR(32) NOT NULL DEFAULT 'ok',
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CONSTRAINT drill_traces_pkey
                    PRIMARY KEY (trace_id),

                CONSTRAINT drill_traces_collar_unique
                    UNIQUE (collar_id),

                CONSTRAINT drill_traces_quality_valid
                    CHECK (trace_quality IN (
                        'ok',
                        'high_dogleg_warning',
                        'single_survey_vertical'
                    )),

                CONSTRAINT drill_traces_collar_id_fkey
                    FOREIGN KEY (collar_id)
                    REFERENCES silver.collars (collar_id)
                    ON DELETE CASCADE,

                CONSTRAINT drill_traces_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,

                CONSTRAINT drill_traces_project_id_fkey
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE
            )
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drill_traces_geom
             ON silver.drill_traces USING GIST (geom);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drill_traces_project
             ON silver.drill_traces (project_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drill_traces_workspace
             ON silver.drill_traces (workspace_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drill_traces_survey_hash
             ON silver.drill_traces (survey_hash);',
        );

        // RLS — match production policy from 2026_05_30_010000
        DB::statement('ALTER TABLE silver.drill_traces ENABLE ROW LEVEL SECURITY;');
        DB::statement('ALTER TABLE silver.drill_traces FORCE ROW LEVEL SECURITY;');

        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON silver.drill_traces;');

        DB::statement(<<<'SQL'
            CREATE POLICY tenant_isolation ON silver.drill_traces
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
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS silver.drill_traces CASCADE;');
    }
};
