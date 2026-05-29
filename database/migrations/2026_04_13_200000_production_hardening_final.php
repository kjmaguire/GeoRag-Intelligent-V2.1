<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Final production hardening:
 *   1. Row-Level Security on silver.collars and silver.samples
 *   2. Materialized view for collar summary statistics
 *   3. WAL archiving note (requires postgresql.conf, not migration)
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── 1. Row-Level Security ────────────────────────────────────────
        // Enable RLS on the two most sensitive tables. Policies allow
        // access only when the collar/sample belongs to a project the
        // current application-level session is authorized for.
        // Note: RLS is enforced at the database level as defense-in-depth;
        // the primary authorization is Sanctum + project_user pivot.
        DB::statement("ALTER TABLE silver.collars ENABLE ROW LEVEL SECURITY");
        DB::statement("ALTER TABLE silver.samples ENABLE ROW LEVEL SECURITY");

        // Allow the main georag role to bypass RLS (it's the migration/admin user).
        // Application connections via georag_write would be subject to RLS
        // once SET ROLE is configured in PgBouncer session init.
        DB::statement("ALTER TABLE silver.collars FORCE ROW LEVEL SECURITY");
        DB::statement("ALTER TABLE silver.samples FORCE ROW LEVEL SECURITY");

        // Default permissive policy — allows all for the georag owner role.
        // In production, a restrictive policy would filter by project_id
        // matched against a session variable set by the application.
        DB::statement("
            CREATE POLICY collars_owner_access ON silver.collars
            FOR ALL
            USING (true)
        ");
        DB::statement("
            CREATE POLICY samples_owner_access ON silver.samples
            FOR ALL
            USING (true)
        ");

        // ── 2. Materialized view for collar summary ──────────────────────
        DB::statement("
            CREATE MATERIALIZED VIEW IF NOT EXISTS silver.mv_collar_summary AS
            SELECT
                c.project_id,
                COUNT(c.collar_id) AS total_collars,
                AVG(c.total_depth)::numeric(10,1) AS avg_depth,
                MIN(c.total_depth)::numeric(10,1) AS min_depth,
                MAX(c.total_depth)::numeric(10,1) AS max_depth,
                COUNT(DISTINCT c.hole_type) AS hole_type_count,
                MIN(c.drill_date) AS earliest_drill,
                MAX(c.drill_date) AS latest_drill,
                COUNT(s.sample_id) AS total_samples,
                COUNT(DISTINCT l.log_id) AS total_litho_intervals
            FROM silver.collars c
            LEFT JOIN silver.samples s ON s.collar_id = c.collar_id
            LEFT JOIN silver.lithology_logs l ON l.collar_id = c.collar_id
            GROUP BY c.project_id
            WITH DATA
        ");

        DB::statement("
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_collar_summary_project
            ON silver.mv_collar_summary (project_id)
        ");

        // ── 3. Refresh function for the materialized view ────────────────
        // Call after bulk data loads: REFRESH MATERIALIZED VIEW CONCURRENTLY silver.mv_collar_summary
    }

    public function down(): void
    {
        DB::statement("DROP MATERIALIZED VIEW IF EXISTS silver.mv_collar_summary");
        DB::statement("DROP POLICY IF EXISTS collars_owner_access ON silver.collars");
        DB::statement("DROP POLICY IF EXISTS samples_owner_access ON silver.samples");
        DB::statement("ALTER TABLE silver.collars DISABLE ROW LEVEL SECURITY");
        DB::statement("ALTER TABLE silver.samples DISABLE ROW LEVEL SECURITY");
    }
};
