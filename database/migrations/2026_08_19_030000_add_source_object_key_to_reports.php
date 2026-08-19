<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Records which bronze object a report was parsed from, so the Reader can
 * show the original page next to the text OCR pulled out of it.
 *
 * Until now nothing on `silver.reports` pointed back at the source PDF.
 * `minio_key` is an *input* to the ingest_pdf workflow and was never
 * persisted alongside the row it produced. The nearest thing was
 * `silver.ingest_progress`, which carries both `minio_key` and `report_id`
 * — but that is a run-tracking table, rows age out, and its `report_id` is
 * only set by `mark_report_id()` on a row that is still non-terminal, so a
 * report whose run had already been marked complete never gets the link
 * (locally: 1,328 progress rows, 0 with a report_id). Reading the source
 * document through it would work sometimes, which is worse than not at all.
 *
 * The backfill below still uses that table because a partial backfill is
 * strictly better than none, and new ingests populate the column directly.
 *
 * Nullable on purpose: reports that predate this and are not covered by the
 * backfill simply have no original to show, and the Reader degrades to the
 * text-only view it has today.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $hasColumn = DB::selectOne(
            "SELECT 1 AS present FROM information_schema.columns
              WHERE table_schema = 'silver' AND table_name = 'reports'
                AND column_name = 'source_object_key'",
        );

        if (! $hasColumn) {
            DB::statement('ALTER TABLE silver.reports ADD COLUMN source_object_key TEXT');
        }

        // Best-effort backfill from run tracking. Guarded because
        // silver.ingest_progress is itself a later addition and may not
        // exist on an older install being upgraded.
        $hasProgress = DB::selectOne(
            "SELECT 1 AS present FROM information_schema.tables
              WHERE table_schema = 'silver' AND table_name = 'ingest_progress'",
        );

        if ($hasProgress) {
            DB::statement(
                'UPDATE silver.reports r
                    SET source_object_key = p.minio_key
                   FROM silver.ingest_progress p
                  WHERE p.report_id = r.report_id
                    AND p.minio_key IS NOT NULL
                    AND r.source_object_key IS NULL',
            );
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.reports DROP COLUMN IF EXISTS source_object_key');
    }
};
