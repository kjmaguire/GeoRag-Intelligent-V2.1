<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB review (Medium — statistics target too low for spatial columns).
 *
 * `default_statistics_target = 100` is the cluster default. At that value
 * the planner samples ~100 buckets to estimate selectivity for any column
 * — fine for low-cardinality enums, lousy for PostGIS GIST and JSONB GIN.
 *
 * Bumping per-column to 1000 on the columns the agent's RAG tools query
 * heavily gives the planner enough resolution to pick the GIST/GIN index
 * over a sequential scan as table size grows past ~100 k rows.
 *
 * - 1000 (hot path):   collars.geom, samples.commodity_assays,
 *                      spatial_features.geom — every spatial agent query.
 * - 500  (secondary):  reports.geom, reports.sections_text,
 *                      reports.resource_estimate, spatial_features.properties.
 * - default (100):     low-volume tables (exports, seismic_surveys.bbox,
 *                      geochemistry.ree_json) where planner accuracy doesn't
 *                      pay back the per-ANALYZE sampling cost.
 *
 * After ALTER TABLE ... SET STATISTICS we MUST run ANALYZE on each touched
 * table for the new sample size to actually populate pg_statistic — the
 * SET STATISTICS itself only changes the *target*, not the current stats.
 */
return new class extends Migration
{
    public function up(): void
    {
        // Hot path — 1000
        DB::statement('ALTER TABLE silver.collars ALTER COLUMN geom SET STATISTICS 1000');
        DB::statement('ALTER TABLE silver.samples ALTER COLUMN commodity_assays SET STATISTICS 1000');
        DB::statement('ALTER TABLE silver.spatial_features ALTER COLUMN geom SET STATISTICS 1000');

        // Secondary — 500
        DB::statement('ALTER TABLE silver.reports ALTER COLUMN geom SET STATISTICS 500');
        DB::statement('ALTER TABLE silver.reports ALTER COLUMN sections_text SET STATISTICS 500');
        DB::statement('ALTER TABLE silver.reports ALTER COLUMN resource_estimate SET STATISTICS 500');
        DB::statement('ALTER TABLE silver.spatial_features ALTER COLUMN properties SET STATISTICS 500');

        // Repopulate pg_statistic with the new sample size. ANALYZE is fast
        // (seconds) on empty/small dev tables and minutes on populated prod
        // tables. Safe to run inline because it acquires a SHARE UPDATE
        // EXCLUSIVE lock — concurrent SELECTs are unaffected.
        DB::statement('ANALYZE silver.collars');
        DB::statement('ANALYZE silver.samples');
        DB::statement('ANALYZE silver.spatial_features');
        DB::statement('ANALYZE silver.reports');
    }

    public function down(): void
    {
        // Revert to cluster default by setting -1 (the magic value
        // documented in pg_attribute.attstattarget meaning "use the
        // cluster-wide default_statistics_target").
        DB::statement('ALTER TABLE silver.collars ALTER COLUMN geom SET STATISTICS -1');
        DB::statement('ALTER TABLE silver.samples ALTER COLUMN commodity_assays SET STATISTICS -1');
        DB::statement('ALTER TABLE silver.spatial_features ALTER COLUMN geom SET STATISTICS -1');
        DB::statement('ALTER TABLE silver.reports ALTER COLUMN geom SET STATISTICS -1');
        DB::statement('ALTER TABLE silver.reports ALTER COLUMN sections_text SET STATISTICS -1');
        DB::statement('ALTER TABLE silver.reports ALTER COLUMN resource_estimate SET STATISTICS -1');
        DB::statement('ALTER TABLE silver.spatial_features ALTER COLUMN properties SET STATISTICS -1');

        DB::statement('ANALYZE silver.collars');
        DB::statement('ANALYZE silver.samples');
        DB::statement('ANALYZE silver.spatial_features');
        DB::statement('ANALYZE silver.reports');
    }
};
