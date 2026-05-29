<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Add commodity_assay_flags JSONB column to preserve lab-reported detection flags.
 *
 * The existing commodity_assays column holds flat numeric payloads (e.g., {"U3O8_ppm": 0.005})
 * that are queried directly with SQL casts. This new column stores per-element metadata
 * (below-detection flags, original string, substitution method) without polluting
 * the queryable assay data. Allows parsers to preserve "<DL" notation without casting
 * float logic. Nullable for backward compatibility with historical rows.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('ALTER TABLE silver.samples ADD COLUMN IF NOT EXISTS commodity_assay_flags JSONB NULL');

        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_samples_assay_flags_gin
            ON silver.samples USING GIN (commodity_assay_flags)
        ');
    }

    public function down(): void
    {
        DB::statement('DROP INDEX IF EXISTS silver.idx_samples_assay_flags_gin');
        DB::statement('ALTER TABLE silver.samples DROP COLUMN IF EXISTS commodity_assay_flags');
    }
};
