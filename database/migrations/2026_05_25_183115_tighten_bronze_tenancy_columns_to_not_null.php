<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Close out the bronze tenancy tightening that started 2026-05-25 with
 * 2026_05_25_170825_enable_rls_on_bronze_tenancy_tables (RLS + nullable
 * workspace_id + IS-NULL exemption) and 2026_05_25_175601 (autofill
 * trigger on bronze.provenance).
 *
 * **What changes here.**
 *
 *   1. Backfill orphan rows on bronze.provenance + bronze.ingest_manifest.
 *      Any row whose workspace_id is still NULL gets tagged with the
 *      Default Workspace UUID. This preserves the audit trail (the row
 *      stays visible to its workspace tenant) rather than deleting it.
 *
 *      bronze.provenance: 1,320 orphan rows (verified 2026-05-25). These
 *      reference targets that no longer exist in the silver tables —
 *      either re-ingested with new UUIDs or stale from a prior run. The
 *      trigger added in 2026_05_25_175601 covers all new inserts so the
 *      orphan set won't regrow.
 *
 *      bronze.ingest_manifest: 39,744 rows from the historical ZIP-walk
 *      pipeline that predates the workspace_id column. The script
 *      (scripts/inspect_ingest_zip.py) was updated 2026-05-25 to require
 *      a --workspace-id flag so new manifests carry it.
 *
 *   2. Update the bronze.provenance autofill trigger ELSE branch to
 *      fall back to Default Workspace when the target lookup fails,
 *      instead of leaving NULL. Without this update the NOT NULL
 *      constraint added below would start rejecting inserts from any
 *      writer that targets a table outside the trigger's known set
 *      (e.g., a new writer added before the trigger is taught about it).
 *
 *   3. Drop the `workspace_id IS NULL` exemption from both RLS policies
 *      so the policies become strict workspace_id-only checks.
 *
 *   4. Add NOT NULL constraint on workspace_id for both tables. Safe
 *      after step 1 leaves zero NULLs.
 *
 * **Default Workspace** is the canonical "unassigned" bucket
 * (a0000000-0000-0000-0000-000000000001), used elsewhere in the codebase
 * (e.g., 96-rls-tenant-isolation-block1.sql backfills orphan reports
 * the same way). Operators can re-scope rows via the admin UI.
 *
 * **Rollback semantics.** down() reverses everything: drops NOT NULL,
 * re-adds the IS NULL exemption, restores the trigger ELSE → NULL
 * behavior. The backfilled workspace_id values stay (we don't track
 * which rows were tagged) — re-running up() is idempotent.
 *
 * SQLite (test DB) does not support RLS or PL/pgSQL — gated on Postgres.
 */
return new class extends Migration
{
    /**
     * The "unassigned / orphan" bucket. Pre-seeded in silver.workspaces
     * by 96-rls-tenant-isolation-block1.sql; safe to reference here.
     */
    private const DEFAULT_WORKSPACE_UUID = 'a0000000-0000-0000-0000-000000000001';

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // ── 1. Backfill orphan rows → Default Workspace ──────────────
        DB::statement(
            'UPDATE bronze.provenance      SET workspace_id = ?::uuid WHERE workspace_id IS NULL',
            [self::DEFAULT_WORKSPACE_UUID],
        );
        DB::statement(
            'UPDATE bronze.ingest_manifest SET workspace_id = ?::uuid WHERE workspace_id IS NULL',
            [self::DEFAULT_WORKSPACE_UUID],
        );

        // ── 2. Update trigger to fall back to Default Workspace ──────
        // CREATE OR REPLACE replaces the function body in place; no
        // need to drop the trigger first.
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION bronze.provenance_autofill_workspace_id()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                resolved_ws uuid;
            BEGIN
                IF NEW.workspace_id IS NOT NULL THEN
                    RETURN NEW;
                END IF;

                CASE NEW.target_schema || '.' || NEW.target_table
                    WHEN 'silver.collars' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.collars WHERE collar_id = NEW.target_id;
                    WHEN 'silver.samples' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.samples WHERE sample_id = NEW.target_id;
                    WHEN 'silver.lithology_logs' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.lithology_logs WHERE log_id = NEW.target_id;
                    WHEN 'silver.reports' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.reports WHERE report_id = NEW.target_id;
                    WHEN 'silver.spatial_features' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.spatial_features WHERE feature_id = NEW.target_id;
                    WHEN 'silver.raster_layers' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.raster_layers WHERE raster_id = NEW.target_id;
                    WHEN 'silver.geophysics_surveys' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.geophysics_surveys
                          WHERE survey_id = NEW.target_id;
                    WHEN 'silver.assays_v2' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.assays_v2 WHERE assay_id = NEW.target_id;
                    ELSE
                        resolved_ws := NULL;
                END CASE;

                -- Fall back to Default Workspace when the lookup either
                -- returned NULL (target row deleted, or unknown target
                -- table) or wasn't attempted. NOT NULL constraint
                -- enforced by the migration that installed this version.
                IF resolved_ws IS NULL THEN
                    resolved_ws := 'a0000000-0000-0000-0000-000000000001'::uuid;
                END IF;

                NEW.workspace_id := resolved_ws;
                RETURN NEW;
            EXCEPTION
                WHEN OTHERS THEN
                    -- Defensive — if the silver target table is missing
                    -- or has a different PK shape, fall back rather
                    -- than failing the provenance INSERT.
                    NEW.workspace_id := 'a0000000-0000-0000-0000-000000000001'::uuid;
                    RETURN NEW;
            END;
            $$;
        SQL);

        // ── 3. Tighten RLS policies — drop IS NULL exemption ─────────
        DB::statement('DROP POLICY IF EXISTS bronze_ingest_manifest_workspace_isolation ON bronze.ingest_manifest');
        DB::statement(<<<'SQL'
            CREATE POLICY bronze_ingest_manifest_workspace_isolation ON bronze.ingest_manifest
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        DB::statement('DROP POLICY IF EXISTS bronze_provenance_workspace_isolation ON bronze.provenance');
        DB::statement(<<<'SQL'
            CREATE POLICY bronze_provenance_workspace_isolation ON bronze.provenance
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        // ── 4. Add NOT NULL constraint ───────────────────────────────
        // Safe — step 1 zeroed out the NULL rows, and the updated
        // trigger keeps future inserts populated.
        DB::statement('ALTER TABLE bronze.ingest_manifest ALTER COLUMN workspace_id SET NOT NULL');
        DB::statement('ALTER TABLE bronze.provenance      ALTER COLUMN workspace_id SET NOT NULL');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // 4. Drop NOT NULL
        DB::statement('ALTER TABLE bronze.ingest_manifest ALTER COLUMN workspace_id DROP NOT NULL');
        DB::statement('ALTER TABLE bronze.provenance      ALTER COLUMN workspace_id DROP NOT NULL');

        // 3. Restore IS NULL exemption on policies
        DB::statement('DROP POLICY IF EXISTS bronze_ingest_manifest_workspace_isolation ON bronze.ingest_manifest');
        DB::statement(<<<'SQL'
            CREATE POLICY bronze_ingest_manifest_workspace_isolation ON bronze.ingest_manifest
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        DB::statement('DROP POLICY IF EXISTS bronze_provenance_workspace_isolation ON bronze.provenance');
        DB::statement(<<<'SQL'
            CREATE POLICY bronze_provenance_workspace_isolation ON bronze.provenance
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        // 2. Restore trigger ELSE → NULL behavior (revert to the
        // 2026_05_25_175601 shape).
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION bronze.provenance_autofill_workspace_id()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                resolved_ws uuid;
            BEGIN
                IF NEW.workspace_id IS NOT NULL THEN
                    RETURN NEW;
                END IF;

                CASE NEW.target_schema || '.' || NEW.target_table
                    WHEN 'silver.collars' THEN
                        SELECT workspace_id INTO resolved_ws FROM silver.collars WHERE collar_id = NEW.target_id;
                    WHEN 'silver.samples' THEN
                        SELECT workspace_id INTO resolved_ws FROM silver.samples WHERE sample_id = NEW.target_id;
                    WHEN 'silver.lithology_logs' THEN
                        SELECT workspace_id INTO resolved_ws FROM silver.lithology_logs WHERE log_id = NEW.target_id;
                    WHEN 'silver.reports' THEN
                        SELECT workspace_id INTO resolved_ws FROM silver.reports WHERE report_id = NEW.target_id;
                    WHEN 'silver.spatial_features' THEN
                        SELECT workspace_id INTO resolved_ws FROM silver.spatial_features WHERE feature_id = NEW.target_id;
                    WHEN 'silver.raster_layers' THEN
                        SELECT workspace_id INTO resolved_ws FROM silver.raster_layers WHERE raster_id = NEW.target_id;
                    WHEN 'silver.geophysics_surveys' THEN
                        SELECT workspace_id INTO resolved_ws FROM silver.geophysics_surveys WHERE survey_id = NEW.target_id;
                    WHEN 'silver.assays_v2' THEN
                        SELECT workspace_id INTO resolved_ws FROM silver.assays_v2 WHERE assay_id = NEW.target_id;
                    ELSE
                        RETURN NEW;
                END CASE;

                NEW.workspace_id := resolved_ws;
                RETURN NEW;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NEW;
            END;
            $$;
        SQL);

        // 1. Backfill NOT reversed — we don't know which rows we tagged.
    }
};
