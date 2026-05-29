<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Evidence Inspector follow-up #3 — backing columns for the
 * lazy-cached page-image + provenance confidence chips.
 *
 * Adds (idempotent, IF NOT EXISTS):
 *   silver.document_passages.parser_confidence  numeric(5,4)
 *   silver.document_passages.bbox_x0 .. bbox_y1 numeric(8,4)
 *   silver.structured_record_lineage.extraction_confidence numeric(5,4)
 *
 * The columns are nullable on purpose — every passage / lineage row
 * ingested before this migration will have NULL values, and the
 * inspector simply omits the confidence chip + bbox highlight when the
 * row carries no value. New ingest runs populate them per the §04p
 * PaddleOCR pipeline (parser_confidence ← OCR confidence, bbox_* ←
 * PaddleOCR layout output normalised to 0–1 page coords) and the
 * structured ingesters (LAS, geochem, etc.) for extraction_confidence.
 *
 * SQLite (test DB) — gated on Postgres.
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
              ADD COLUMN IF NOT EXISTS parser_confidence numeric(5,4),
              ADD COLUMN IF NOT EXISTS bbox_x0 numeric(8,4),
              ADD COLUMN IF NOT EXISTS bbox_y0 numeric(8,4),
              ADD COLUMN IF NOT EXISTS bbox_x1 numeric(8,4),
              ADD COLUMN IF NOT EXISTS bbox_y1 numeric(8,4)
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.parser_confidence IS
              'PaddleOCR / Docling parser confidence for the passage, 0.0–1.0. NULL = passage came from a text-extractable PDF and no OCR ran.'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.bbox_x0 IS
              'Normalised 0–1 X-min of the passage on its source page. Together with bbox_y0/x1/y1 these power the Evidence Inspector bbox highlight overlay.'
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.structured_record_lineage
              ADD COLUMN IF NOT EXISTS extraction_confidence numeric(5,4)
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.structured_record_lineage.extraction_confidence IS
              'Per-record extraction confidence emitted by the structured ingester (LAS, geochem CSV, assay JSON, etc.). 0.0–1.0. NULL when the ingester did not report a confidence.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              DROP COLUMN IF EXISTS parser_confidence,
              DROP COLUMN IF EXISTS bbox_x0,
              DROP COLUMN IF EXISTS bbox_y0,
              DROP COLUMN IF EXISTS bbox_x1,
              DROP COLUMN IF EXISTS bbox_y1
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.structured_record_lineage
              DROP COLUMN IF EXISTS extraction_confidence
        SQL);
    }
};
