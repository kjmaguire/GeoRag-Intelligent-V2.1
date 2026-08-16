<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Closes the verify_numerical_claim dead-entry gap flagged in the
 * 2026-08-15 audit (app/agent/tools.py:2358-2368): silver.reports had no
 * real NUMERIC column, so its allowlist entry was left empty and every
 * factual_lookup / verify call against a report's numeric field was
 * fail-closed BLOCKED.
 *
 * page_count is already computed during PDF ingestion (pikepdf page count
 * in app/hatchet_workflows/ingest_pdf.py's preflight step, pdfminer page
 * count in app/services/ingest/pdf_ingester.py) but was previously
 * discarded after the parse instead of persisted. Nullable — legacy rows
 * ingested before this migration stay NULL until their next re-parse.
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
                ADD COLUMN IF NOT EXISTS page_count INTEGER
                CHECK (page_count IS NULL OR page_count >= 0)
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.reports DROP COLUMN IF EXISTS page_count');
    }
};
