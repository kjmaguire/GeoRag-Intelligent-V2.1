<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Record the unit silver.well_log_curves depths are stored in (GIS-4, §04e).
 *
 * WHY
 *   Neither LAS path read the depth unit the LAS header declares (DEPT.F /
 *   STRT.M), so curves were stored in the file's native unit with nothing
 *   saying which, and derive_intervals multiplied every depth by 0.3048 on
 *   the assumption of feet. A metric log therefore had its derived intervals
 *   drawn at 30% of their true depth, and a feet log's collar total_depth was
 *   3.28x too deep.
 *
 *   Both LAS writers now normalise to metres at ingest and stamp 'm'. Rows
 *   written before this migration stay NULL: their unit was never recorded
 *   and cannot be recovered from the row, so derive_intervals skips them
 *   (``depth_unit_unknown``) instead of guessing. Re-ingesting the LAS
 *   fills it in.
 *
 * Nullable, no default, no backfill — a default of 'm' would claim a unit
 * for legacy rows that nobody knows. Additive only; RLS unchanged.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.well_log_curves ADD COLUMN IF NOT EXISTS depth_unit varchar(8)');
        DB::statement('ALTER TABLE silver.well_log_curves DROP CONSTRAINT IF EXISTS chk_well_log_curves_depth_unit');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.well_log_curves
              ADD CONSTRAINT chk_well_log_curves_depth_unit
              CHECK (depth_unit IS NULL OR depth_unit IN ('m', 'ft'))
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.well_log_curves.depth_unit IS
              'Unit of depths / min_depth / max_depth / step as stored. ''m'' for every row written since 2026-09-29 (the LAS writers normalise to metres from the header unit). NULL = legacy row in the LAS file''s unrecorded native unit; derive_intervals skips it. GIS-4.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.well_log_curves DROP CONSTRAINT IF EXISTS chk_well_log_curves_depth_unit');
        DB::statement('ALTER TABLE silver.well_log_curves DROP COLUMN IF EXISTS depth_unit');
    }
};
