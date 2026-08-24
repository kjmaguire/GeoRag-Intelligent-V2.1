<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Let an ingest run say "finished, but nothing landed".
 *
 * The three non-PDF ingest workflows -- ingest_spatial, ingest_tabular and
 * ingest_well_logs -- called mark_completed_by_run on the success path
 * regardless of whether a single row was written, and every diagnostic they
 * collected lived only in the Hatchet run output, which the product UI never
 * reads. silver.ingest_progress had nowhere to put either fact.
 *
 * So: a geologist uploads GAMMA_EL-001.las before the collar file exists.
 * _resolve_collar returns None, zero curves are written, and a
 * `no_matching_collar` warning is appended to a local Python list whose
 * message contains the actual fix -- "upload the collar file first, or pass
 * hole_id explicitly". The Ingestion Runs page shows a green Completed row.
 * The same shape covers a zipped shapefile whose every member throws, and a
 * CSV whose headers do not classify.
 *
 * Three additions:
 *
 *   rows_written  -- what the run actually produced. NULL for older rows and
 *                    for workflows that have not been taught to report it,
 *                    which is honestly different from 0.
 *   warnings      -- the diagnostics array, so the actionable text reaches
 *                    the UI instead of dying in a Hatchet run object.
 *   'partial'     -- a status for "some rows landed, and something also went
 *                    wrong". Without it the choice was a green Completed or
 *                    a red Failed, and neither is true.
 *
 * All three are additive. Widening a CHECK constraint accepts strictly more
 * than before, so existing rows cannot violate it.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                ADD COLUMN IF NOT EXISTS rows_written integer,
                ADD COLUMN IF NOT EXISTS warnings     jsonb NOT NULL DEFAULT '[]'::jsonb
        SQL);

        DB::statement(
            'ALTER TABLE silver.ingest_progress '
            .'DROP CONSTRAINT IF EXISTS ingest_progress_status_valid',
        );
        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                ADD CONSTRAINT ingest_progress_status_valid
                CHECK (status IN (
                    'queued','started','completed','partial',
                    'failed','cancelled','timed_out'
                ))
        SQL);

        // The UI's "needs attention" query is `terminal AND (failed OR
        // produced nothing)`. Partial index because the overwhelming
        // majority of rows are clean completions, and this table absorbs a
        // heartbeat UPDATE every 30 seconds per active run.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_ingest_progress_needs_attention
                ON silver.ingest_progress (workspace_id, updated_at DESC)
             WHERE status IN ('partial','failed','timed_out')
                OR rows_written = 0
        SQL);
    }

    public function down(): void
    {
        DB::statement('DROP INDEX IF EXISTS silver.idx_ingest_progress_needs_attention');

        // Reverse order: rows must stop using 'partial' before the CHECK
        // that forbids it is restored, or the ALTER fails validation.
        DB::statement(
            "UPDATE silver.ingest_progress SET status = 'completed' WHERE status = 'partial'",
        );
        DB::statement(
            'ALTER TABLE silver.ingest_progress '
            .'DROP CONSTRAINT IF EXISTS ingest_progress_status_valid',
        );
        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                ADD CONSTRAINT ingest_progress_status_valid
                CHECK (status IN (
                    'queued','started','completed','failed','cancelled','timed_out'
                ))
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                DROP COLUMN IF EXISTS rows_written,
                DROP COLUMN IF EXISTS warnings
        SQL);
    }
};
