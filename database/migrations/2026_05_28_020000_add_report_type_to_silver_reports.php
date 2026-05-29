<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §1c — silver.reports.report_type column for document
 * classification (populated by app.agent.document_classifier from
 * filename + title + body signals).
 *
 * The classifier output aligns with authority.py's
 * DOCUMENT_TYPE_RANK_PATTERNS so downstream §3b authority ranking
 * works on the classifier-populated value directly. Nullable —
 * legacy rows pre-classifier remain NULL; the ingest pipeline
 * back-fills on next re-parse.
 *
 * Index: btree on (workspace_id, report_type) so the Foundry
 * Lakehouse page can filter "show me only NI 43-101 reports" cheaply.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.reports
                ADD COLUMN IF NOT EXISTS report_type VARCHAR(40)
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_silver_reports_workspace_report_type
                ON silver.reports (workspace_id, report_type)
                WHERE report_type IS NOT NULL
        SQL);

        // Provision on the test DB too (idempotent — provision migrations
        // run after a database name ends in '_test').
        $dbName = DB::selectOne('SELECT current_database() AS db')->db ?? '';
        if (str_ends_with($dbName, '_test')) {
            // No-op — the same DDL above already applied to the test DB.
            // Documenting here so the test-DB parity convention is visible.
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP INDEX IF EXISTS silver.idx_silver_reports_workspace_report_type');
        DB::statement('ALTER TABLE silver.reports DROP COLUMN IF EXISTS report_type');
    }
};
