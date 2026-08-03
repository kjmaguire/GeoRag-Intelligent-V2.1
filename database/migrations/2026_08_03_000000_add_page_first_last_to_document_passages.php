<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Adds the page-range columns that app/hatchet_workflows/ingest_pdf.py's
 * INSERT_PASSAGE_SQL, app/services/ingest/pdf_ingester.py, and
 * app/services/ingest/passage_embedder.py have always assumed exist on
 * silver.document_passages — a migration for these was never written,
 * so every ingest run's `persist` step failed with UndefinedColumnError
 * the first time it hit a fully-migrated database.
 *
 *   page_first  integer  — first 1-indexed PDF page this passage spans.
 *   page_last   integer  — last 1-indexed PDF page this passage spans.
 *
 * Both nullable: a passage built outside the per-page PDF path (e.g. a
 * synthesized "section" chunk grouping several page-level children) still
 * derives these from its children, but any passage predating this column
 * has no way to backfill the value and stays NULL.
 *
 * SQLite (test DB) — gated; column additions are a no-op when running on
 * the in-memory test DB. The in-prod silver schema is Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              ADD COLUMN IF NOT EXISTS page_first integer,
              ADD COLUMN IF NOT EXISTS page_last  integer
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.page_first IS
              'First 1-indexed PDF page this passage spans. NULL when the passage predates page-range tracking or was not built from a per-page PDF source.'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.page_last IS
              'Last 1-indexed PDF page this passage spans. NULL under the same conditions as page_first.'
        SQL);

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'document_passages_page_range_positive'
                ) THEN
                    ALTER TABLE silver.document_passages
                      ADD CONSTRAINT document_passages_page_range_positive
                      CHECK (
                        (page_first IS NULL OR page_first >= 1)
                        AND (page_last IS NULL OR page_last >= 1)
                        AND (page_first IS NULL OR page_last IS NULL OR page_last >= page_first)
                      );
                END IF;
            END $$
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              DROP CONSTRAINT IF EXISTS document_passages_page_range_positive
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              DROP COLUMN IF EXISTS page_first,
              DROP COLUMN IF EXISTS page_last
        SQL);
    }
};
