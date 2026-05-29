<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Grant DELETE on the three lithology-derive output tables to georag_app.
 *
 * `derive_intervals.py` is re-runnable by deleting any prior DERIVED-*
 * rows for the collar before re-inserting. The fastapi runtime connects
 * as `georag_app`, which previously had only INSERT/SELECT/UPDATE on
 * these tables, so every per-collar derive call failed with
 * `permission denied for table lithology_logs`. The result: 302/302
 * collars skipped, 0 intervals derived across the 120-project Wyoming
 * corpus.
 *
 * Granting DELETE here is safe because:
 *   - RLS on these tables already restricts visibility / DML to the
 *     calling workspace (app.workspace_id GUC)
 *   - The derive code's WHERE clause limits the blast radius to
 *     `lithology_code LIKE 'DERIVED-%'` and
 *     `sample_type = 'derived_composite'` and
 *     `interval_kind = 'lithology'` respectively, so manually-imported
 *     rows are not touched
 *
 * Skipped under SQLite (test DB has no Postgres roles).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('GRANT DELETE ON silver.lithology_logs TO georag_app');
        DB::statement('GRANT DELETE ON silver.samples TO georag_app');
        DB::statement('GRANT DELETE ON gold.drillhole_intervals_visual TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('REVOKE DELETE ON silver.lithology_logs FROM georag_app');
        DB::statement('REVOKE DELETE ON silver.samples FROM georag_app');
        DB::statement('REVOKE DELETE ON gold.drillhole_intervals_visual FROM georag_app');
    }
};
