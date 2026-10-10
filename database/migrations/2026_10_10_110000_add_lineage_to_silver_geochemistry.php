<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Inline source lineage on silver.geochemistry.
 *
 * Why: ingest_tabular upserts surface samples on (project_id, sample_id), so a
 * second file that reuses a sample number replaced the first file's row, and
 * nothing on the row said which file had written it - a geochemistry sample
 * could not be traced to its source, and the replacement could not be reported.
 * Every sibling table (the interval tables, assays_v2, attribute_tables,
 * geochronology_samples, spatial_features) already carries the same columns.
 *
 *   source_file         the logical file name, WITHOUT the upload timestamp
 *                       prefix (the replace key everywhere else).
 *   source_file_sha256  the content hash of the uploaded bytes.
 *   row_index           0-based position of the row in the source table - the
 *                       same index silver.attribute_tables.row_index uses for
 *                       the verbatim copy, so the two can be joined.
 *
 * All nullable and not backfilled: rows written before this migration carry
 * NULL, and inventing a source for them would be the guess the columns exist
 * to avoid. ingest_tabular treats a NULL source as "unknown" when it decides
 * whether an upsert replaced another file's row.
 *
 * Additive and reversible. pgsql only.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement("SET LOCAL lock_timeout = '15s'");
        DB::statement('ALTER TABLE silver.geochemistry ADD COLUMN IF NOT EXISTS source_file text');
        DB::statement('ALTER TABLE silver.geochemistry ADD COLUMN IF NOT EXISTS source_file_sha256 varchar(64)');
        DB::statement('ALTER TABLE silver.geochemistry ADD COLUMN IF NOT EXISTS row_index integer');
        DB::statement("COMMENT ON COLUMN silver.geochemistry.source_file IS 'Logical name (no upload timestamp) of the file that last wrote this row. NULL = written before lineage was kept.'");
        DB::statement("COMMENT ON COLUMN silver.geochemistry.source_file_sha256 IS 'SHA-256 of the uploaded bytes that last wrote this row.'");
        DB::statement("COMMENT ON COLUMN silver.geochemistry.row_index IS '0-based row position in the source table; the same index as silver.attribute_tables.row_index.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement("SET LOCAL lock_timeout = '15s'");
        DB::statement('ALTER TABLE silver.geochemistry DROP COLUMN IF EXISTS row_index');
        DB::statement('ALTER TABLE silver.geochemistry DROP COLUMN IF EXISTS source_file_sha256');
        DB::statement('ALTER TABLE silver.geochemistry DROP COLUMN IF EXISTS source_file');
    }
};
