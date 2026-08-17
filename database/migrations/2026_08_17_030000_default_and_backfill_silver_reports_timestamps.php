<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.reports.created_at / updated_at have never had a column default,
 * and INSERT_REPORT_SQL in ingest_pdf.py never listed them in its column
 * list either — so every report inserted through the live path has been
 * getting NULL for both. Found live 2026-08-17 while investigating why
 * the IngestQuality/Sources pages' "latest reports" queries (ORDER BY
 * created_at DESC) don't reflect real upload order: 35 of the table's
 * rows had NULL created_at, 34 had NULL updated_at.
 *
 * ingest_pdf.py's INSERT_REPORT_SQL now explicitly writes NOW() for both
 * on insert (belt-and-suspenders); this migration adds the column
 * default so any other/future writer can't reintroduce the gap, and
 * backfills existing NULLs. Backfill uses NOW() for both — there's no
 * other timestamp column on this table to recover the true historical
 * ingestion time from, so this is a one-time "when we noticed" stamp,
 * not a recovered "when it happened" stamp.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.reports ALTER COLUMN created_at SET DEFAULT NOW()');
        DB::statement('ALTER TABLE silver.reports ALTER COLUMN updated_at SET DEFAULT NOW()');
        DB::statement('UPDATE silver.reports SET created_at = NOW() WHERE created_at IS NULL');
        DB::statement('UPDATE silver.reports SET updated_at = NOW() WHERE updated_at IS NULL');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.reports ALTER COLUMN created_at DROP DEFAULT');
        DB::statement('ALTER TABLE silver.reports ALTER COLUMN updated_at DROP DEFAULT');
    }
};
