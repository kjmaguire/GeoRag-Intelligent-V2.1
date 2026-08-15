<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB dimension push-to-9.5 sweep (2026-08-15) — missing FK-supporting index.
 *
 * 2026_08_14_020000_add_fks_to_ingest_progress_and_review_queue.php added
 * `ingest_progress_report_id_fk` (silver.ingest_progress.report_id ->
 * silver.reports.report_id ON DELETE SET NULL) but no index backs the
 * child-side column. The workspace_id and project_id FKs on the same
 * migration are covered incidentally — workspace_id by the
 * ingest_progress_workspace_key_uniq UNIQUE (workspace_id, minio_key)
 * index, project_id by idx_ingest_progress_project_all /
 * idx_ingest_progress_project_active — but report_id has no covering
 * index anywhere in the migration chain.
 *
 * Without one, every `DELETE FROM silver.reports` triggers a sequential
 * scan of silver.ingest_progress to find rows to null out (ON DELETE SET
 * NULL), and it's a table Postgres re-scans on every report deletion for
 * the lifetime of the app. report_id is NULL for the majority of rows
 * (only back-filled on the final ingest step per the table's docstring),
 * so a partial index keeps it small and cheap to maintain against the
 * per-30s heartbeat UPDATEs this table absorbs.
 *
 * ADD INDEX CONCURRENTLY cannot run inside Laravel's migration
 * transaction wrapper, so this uses a plain CREATE INDEX (brief
 * SHARE lock, acceptable for a table this size — see the pending_embed
 * index migration, 2026_08_15_010000, for the same tradeoff already
 * accepted on this table).
 *
 * Test-DB parity: no *_provision_*_for_test_db sibling needed — this only
 * adds an index to a table the ordinary migration chain already creates
 * (2026_05_24_230000_create_silver_ingest_progress.php), so the pgsql
 * test DB gets it automatically via the normal migrate chain.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_progress_report_id
             ON silver.ingest_progress (report_id)
             WHERE report_id IS NOT NULL',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_ingest_progress_report_id');
    }
};
