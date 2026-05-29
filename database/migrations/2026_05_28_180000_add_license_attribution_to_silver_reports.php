<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §6c — OER textbook ingest preparation.
 *
 * Adds per-row license / attribution columns to silver.reports so any
 * external content (open educational resources, public-domain
 * government publications, third-party reference texts) can carry its
 * own provenance independent of the workspace's NI 43-101 corpus.
 *
 * Columns:
 *
 *   license            — SPDX-style identifier (e.g. CC-BY-4.0,
 *                        CC-BY-SA-4.0, public-domain). NULL on
 *                        legacy rows (NI 43-101 filings have implicit
 *                        ownership the workspace already controls).
 *
 *   license_url        — Canonical URL for the licence text. e.g.
 *                        https://creativecommons.org/licenses/by/4.0/
 *                        — surfaced in chat citations so reviewers can
 *                        verify the licence claim.
 *
 *   attribution_text   — Free-text string the citation renderer
 *                        embeds verbatim. BCcampus convention is:
 *                        "Physical Geology – 2nd Edition by Steven Earle,
 *                         licensed under CC-BY 4.0"
 *
 *   source_url         — URL where the original was downloaded from.
 *                        Helps both human verification + content
 *                        update detection on re-ingest.
 *
 * These are forward-compatible for future OER / open-government /
 * public-domain content. The first consumer is Earle's Physical
 * Geology textbook ingest (Plan §6c starter).
 *
 * Per the test-DB parity convention from MEMORY:project_test_db_parity_gap,
 * any silver-table column addition reaches the test DB through this
 * same migration run (silver.reports is in the test DB schema).
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
                ADD COLUMN IF NOT EXISTS license          VARCHAR(40),
                ADD COLUMN IF NOT EXISTS license_url      TEXT,
                ADD COLUMN IF NOT EXISTS attribution_text TEXT,
                ADD COLUMN IF NOT EXISTS source_url       TEXT
        SQL);

        // Helpful filter for "show me only externally-licensed content"
        // in the Lakehouse / corpus inspection views. Partial index
        // because the column is NULL on the dominant NI 43-101 path.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_silver_reports_license
                ON silver.reports (license)
                WHERE license IS NOT NULL
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP INDEX IF EXISTS silver.idx_silver_reports_license');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.reports
                DROP COLUMN IF EXISTS source_url,
                DROP COLUMN IF EXISTS attribution_text,
                DROP COLUMN IF EXISTS license_url,
                DROP COLUMN IF EXISTS license
        SQL);
    }
};
