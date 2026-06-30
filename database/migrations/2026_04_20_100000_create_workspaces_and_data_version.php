<?php

/**
 * B1 — DVER: Create workspaces table and add data_version to projects.
 *
 * Implements Global Invariant 12 (data_version monotonic counter) per addendum §05d.
 * Module 3 Phase B 2026-04-20.
 *
 * Changes:
 *   1. Create silver.workspaces with data_version BIGINT and monotonic trigger.
 *   2. Seed a default workspace row so existing projects can migrate into it.
 *   3. Add workspace_id UUID FK (nullable) and data_version BIGINT to silver.projects.
 *   4. Backfill existing projects.workspace_id → default workspace.
 *   5. Attach monotonic trigger on silver.projects.data_version.
 *
 * Default workspace UUID: a0000000-0000-0000-0000-000000000001
 * Recorded here for Module 9 RBAC. Do not change once seeded.
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    /** Stable UUID for the default workspace seeded in this migration. */
    private string $defaultWorkspaceId = 'a0000000-0000-0000-0000-000000000001';

    public function up(): void
    {
        // -----------------------------------------------------------------------
        // 1. Create silver.workspaces
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.workspaces (
                workspace_id  UUID         NOT NULL DEFAULT gen_random_uuid(),
                name          VARCHAR(255) NOT NULL,
                slug          VARCHAR(255) NOT NULL,
                data_version  BIGINT       NOT NULL DEFAULT 0,
                created_at    TIMESTAMP(0) WITHOUT TIME ZONE,
                updated_at    TIMESTAMP(0) WITHOUT TIME ZONE,
                CONSTRAINT workspaces_pkey PRIMARY KEY (workspace_id),
                CONSTRAINT workspaces_slug_unique UNIQUE (slug)
            )',
        );

        // -----------------------------------------------------------------------
        // 2. Shared monotonic-guard function (used by both triggers below)
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE OR REPLACE FUNCTION silver.enforce_data_version_monotonic()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.data_version < OLD.data_version THEN
                    RAISE EXCEPTION
                        \'data_version is monotonic — cannot decrement from % to %\',
                        OLD.data_version,
                        NEW.data_version;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql',
        );

        // -----------------------------------------------------------------------
        // 3. Monotonic trigger on silver.workspaces
        // -----------------------------------------------------------------------
        DB::statement('DROP TRIGGER IF EXISTS workspaces_data_version_monotonic ON silver.workspaces');

        DB::statement(
            'CREATE TRIGGER workspaces_data_version_monotonic
                BEFORE UPDATE ON silver.workspaces
                FOR EACH ROW
                WHEN (NEW.data_version IS DISTINCT FROM OLD.data_version)
                EXECUTE FUNCTION silver.enforce_data_version_monotonic()',
        );

        // -----------------------------------------------------------------------
        // 4. Seed the default workspace
        // -----------------------------------------------------------------------
        DB::statement(
            "INSERT INTO silver.workspaces (workspace_id, name, slug, data_version, created_at, updated_at)
             VALUES (
                 '{$this->defaultWorkspaceId}',
                 'Default Workspace',
                 'default',
                 0,
                 NOW(),
                 NOW()
             )
             ON CONFLICT (workspace_id) DO NOTHING",
        );

        // -----------------------------------------------------------------------
        // 5. Add workspace_id (nullable FK) and data_version to silver.projects
        // -----------------------------------------------------------------------
        DB::statement(
            'ALTER TABLE silver.projects
                ADD COLUMN IF NOT EXISTS workspace_id UUID NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE SET NULL,
                ADD COLUMN IF NOT EXISTS data_version BIGINT NOT NULL DEFAULT 0',
        );

        // -----------------------------------------------------------------------
        // 6. Backfill existing projects → default workspace
        // -----------------------------------------------------------------------
        DB::statement(
            "UPDATE silver.projects
                SET workspace_id = '{$this->defaultWorkspaceId}'
              WHERE workspace_id IS NULL",
        );

        // -----------------------------------------------------------------------
        // 7. Monotonic trigger on silver.projects
        // -----------------------------------------------------------------------
        DB::statement('DROP TRIGGER IF EXISTS projects_data_version_monotonic ON silver.projects');

        DB::statement(
            'CREATE TRIGGER projects_data_version_monotonic
                BEFORE UPDATE ON silver.projects
                FOR EACH ROW
                WHEN (NEW.data_version IS DISTINCT FROM OLD.data_version)
                EXECUTE FUNCTION silver.enforce_data_version_monotonic()',
        );
    }

    public function down(): void
    {
        // Remove triggers and columns from projects first (FK child)
        DB::statement('DROP TRIGGER IF EXISTS projects_data_version_monotonic ON silver.projects');
        DB::statement('ALTER TABLE silver.projects DROP COLUMN IF EXISTS data_version');
        DB::statement('ALTER TABLE silver.projects DROP COLUMN IF EXISTS workspace_id');

        // Drop workspaces table and its trigger
        DB::statement('DROP TRIGGER IF EXISTS workspaces_data_version_monotonic ON silver.workspaces');
        DB::statement('DROP TABLE IF EXISTS silver.workspaces');

        // Drop shared function only after both triggers are removed
        DB::statement('DROP FUNCTION IF EXISTS silver.enforce_data_version_monotonic()');
    }
};
