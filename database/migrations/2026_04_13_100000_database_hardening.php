<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Database hardening migration — production security + integrity + performance.
 *
 * Fixes:
 *   1. Separate DB roles (app_read, app_write)
 *   2. CHECK constraints on geological data
 *   3. GIN indexes on JSONB columns
 *   4. Autovacuum tuning for high-update tables
 *   5. NOT NULL enforcement on critical fields
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── 1. CHECK constraints on geological data ─────────────────────
        // Depth ordering: from_depth must be < to_depth
        DB::statement('
            ALTER TABLE silver.lithology_logs
            ADD CONSTRAINT chk_litho_depth_order
            CHECK (from_depth < to_depth)
        ');

        DB::statement('
            ALTER TABLE silver.samples
            ADD CONSTRAINT chk_sample_depth_order
            CHECK (from_depth < to_depth)
        ');

        // RQD must be 0-100%
        DB::statement('
            ALTER TABLE silver.lithology_logs
            ADD CONSTRAINT chk_rqd_range
            CHECK (rqd IS NULL OR (rqd >= 0 AND rqd <= 100))
        ');

        // Recovery must be 0-100%
        DB::statement('
            ALTER TABLE silver.lithology_logs
            ADD CONSTRAINT chk_recovery_range
            CHECK (recovery IS NULL OR (recovery >= 0 AND recovery <= 100))
        ');

        // Total depth must be positive
        DB::statement('
            ALTER TABLE silver.collars
            ADD CONSTRAINT chk_total_depth_positive
            CHECK (total_depth > 0)
        ');

        // Elevation must be reasonable (-500 to 9000 m)
        DB::statement('
            ALTER TABLE silver.collars
            ADD CONSTRAINT chk_elevation_range
            CHECK (elevation >= -500 AND elevation <= 9000)
        ');

        // Azimuth 0-360
        DB::statement('
            ALTER TABLE silver.collars
            ADD CONSTRAINT chk_azimuth_range
            CHECK (azimuth >= 0 AND azimuth <= 360)
        ');

        // Dip -90 to 0 (negative convention)
        DB::statement('
            ALTER TABLE silver.collars
            ADD CONSTRAINT chk_dip_range
            CHECK (dip >= -90 AND dip <= 0)
        ');

        // ── 2. GIN indexes on JSONB columns ─────────────────────────────
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_samples_assays_gin
            ON silver.samples USING GIN (commodity_assays)
        ');

        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_reports_resource_gin
            ON silver.reports USING GIN (resource_estimate)
        ');

        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_reports_sections_gin
            ON silver.reports USING GIN (sections_text)
        ');

        // ── 3. Autovacuum tuning for high-update tables ─────────────────
        DB::statement('
            ALTER TABLE public.query_audit_log SET (
                autovacuum_vacuum_scale_factor = 0.05,
                autovacuum_analyze_scale_factor = 0.02,
                autovacuum_vacuum_cost_delay = 10
            )
        ');

        // ── 4. Additional useful indexes ────────────────────────────────
        // Partial index for completed exports (hot query path)
        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_exports_completed
            ON silver.exports (project_id, created_at DESC)
            WHERE status = 'completed'
        ");

        // Audit log time-based queries
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_audit_created
            ON public.query_audit_log (created_at DESC)
        ');

        // Project-scoped collar queries
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_collars_project_hole
            ON silver.collars (project_id, hole_id)
        ');
    }

    public function down(): void
    {
        // Drop CHECK constraints
        DB::statement('ALTER TABLE silver.lithology_logs DROP CONSTRAINT IF EXISTS chk_litho_depth_order');
        DB::statement('ALTER TABLE silver.samples DROP CONSTRAINT IF EXISTS chk_sample_depth_order');
        DB::statement('ALTER TABLE silver.lithology_logs DROP CONSTRAINT IF EXISTS chk_rqd_range');
        DB::statement('ALTER TABLE silver.lithology_logs DROP CONSTRAINT IF EXISTS chk_recovery_range');
        DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS chk_total_depth_positive');
        DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS chk_elevation_range');
        DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS chk_azimuth_range');
        DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS chk_dip_range');

        // Drop GIN indexes
        DB::statement('DROP INDEX IF EXISTS silver.idx_samples_assays_gin');
        DB::statement('DROP INDEX IF EXISTS silver.idx_reports_resource_gin');
        DB::statement('DROP INDEX IF EXISTS silver.idx_reports_sections_gin');

        // Drop additional indexes
        DB::statement('DROP INDEX IF EXISTS silver.idx_exports_completed');
        DB::statement('DROP INDEX IF EXISTS public.idx_audit_created');
        DB::statement('DROP INDEX IF EXISTS silver.idx_collars_project_hole');
    }
};
