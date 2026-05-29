<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB review #5 — drop four duplicate indexes that both Laravel migrations
 * and Dagster ingestion assets were creating under different names.
 *
 * Keeping: idx_<table>_<col>   (Laravel-migration convention)
 * Dropping: <table>_<col>_idx  (Dagster DO-block convention)
 *
 * Every duplicate pair was byte-identical in `pg_indexes.indexdef`; the
 * planner picked one and ignored the other, but every INSERT/UPDATE/DELETE
 * had to maintain both — doubling write cost on hot ingestion paths.
 *
 * Uses `DROP INDEX IF EXISTS` so the migration is idempotent on databases
 * where one or more duplicates were already dropped manually.
 *
 * NOTE: `DROP INDEX CONCURRENTLY` cannot run inside a transaction block and
 * Laravel wraps each migration in a transaction by default — so we set
 * `$withinTransaction = false` and rely on low contention on a duplicate
 * index (nothing queries it; dropping it takes an AccessExclusiveLock for
 * microseconds because the index is already offline from the planner's POV).
 */
return new class extends Migration
{
    public $withinTransaction = false;

    public function up(): void
    {
        DB::statement('DROP INDEX CONCURRENTLY IF EXISTS silver.collars_geom_idx');
        DB::statement('DROP INDEX CONCURRENTLY IF EXISTS silver.reports_geom_idx');
        DB::statement('DROP INDEX CONCURRENTLY IF EXISTS silver.samples_assays_gin_idx');
        DB::statement('DROP INDEX CONCURRENTLY IF EXISTS silver.spatial_features_geom_idx');
    }

    public function down(): void
    {
        // Intentionally no-op. Re-creating the duplicates would re-introduce
        // the double-write penalty we just removed. If you truly need to
        // revert, drop the surviving idx_* index instead and rename the
        // duplicate back — but ask what you're actually trying to undo.
    }
};
