<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 3 (2026-05-22) — OCR confidence scoring end-to-end.
 *
 * Adds two new nullable columns to silver.document_passages that travel
 * with each chunk all the way to qdrant payload + search_documents:
 *
 *   ocr_confidence  numeric(5,4)  — 0.0–1.0 OCR engine confidence
 *                                   NULL when the passage came from
 *                                   the PDF text layer (fitz/pdfplumber
 *                                   native, no OCR involved).
 *   ocr_method      varchar(50)   — which engine produced the text:
 *                                     fitz_native        (no OCR)
 *                                     pdfplumber_native  (no OCR fallback)
 *                                     docling_rapidocr   (Phase 2.0+)
 *                                     tesseract          (Phase 2.1 fallback)
 *
 * Constraints:
 *   - ocr_confidence must be NULL or in [0, 1]
 *   - ocr_method must be NULL or one of the four allowed values
 *
 * Partial index for the Phase 6 OCR Quality Agent (looks up
 * low-confidence passages fast without scanning the whole table).
 *
 * Distinct from the pre-existing `parser_confidence` column (added
 * 2026-05-20_040000): that is overall parser confidence (PaddleOCR /
 * docling layout score). The Phase 3 columns specifically capture the
 * OCR engine + per-passage OCR confidence and travel with the qdrant
 * vector for retrieval-time use. The two coexist with NULL semantics
 * meaning "this category does not apply to this passage".
 *
 * SQLite (test DB) — gated; column additions are a no-op when running
 * on the in-memory test DB. The in-prod silver schema is Postgres.
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
              ADD COLUMN IF NOT EXISTS ocr_confidence numeric(5,4),
              ADD COLUMN IF NOT EXISTS ocr_method     varchar(50)
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.ocr_confidence IS
              'Phase 3 — per-passage OCR engine confidence, 0.0–1.0. NULL means the passage came from the PDF text layer (no OCR involved). Travels with the qdrant payload so retrieval can weight low-confidence chunks down.'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.ocr_method IS
              'Phase 3 — which engine produced the text: fitz_native, pdfplumber_native, docling_rapidocr, or tesseract. NULL when extraction predates Phase 3.'
        SQL);

        // Range constraint on ocr_confidence
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'document_passages_ocr_confidence_range'
                ) THEN
                    ALTER TABLE silver.document_passages
                      ADD CONSTRAINT document_passages_ocr_confidence_range
                      CHECK (ocr_confidence IS NULL
                          OR (ocr_confidence >= 0 AND ocr_confidence <= 1));
                END IF;
            END $$
        SQL);

        // Enum constraint on ocr_method
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'document_passages_ocr_method_check'
                ) THEN
                    ALTER TABLE silver.document_passages
                      ADD CONSTRAINT document_passages_ocr_method_check
                      CHECK (ocr_method IS NULL OR ocr_method IN (
                        'fitz_native',
                        'pdfplumber_native',
                        'docling_rapidocr',
                        'tesseract'
                      ));
                END IF;
            END $$
        SQL);

        // Partial index — Phase 6 OCR Quality Agent target
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_document_passages_low_ocr_confidence
              ON silver.document_passages (workspace_id, document_id, ocr_confidence)
              WHERE ocr_confidence IS NOT NULL AND ocr_confidence < 0.75
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            DROP INDEX IF EXISTS silver.idx_document_passages_low_ocr_confidence
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              DROP CONSTRAINT IF EXISTS document_passages_ocr_confidence_range
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              DROP CONSTRAINT IF EXISTS document_passages_ocr_method_check
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              DROP COLUMN IF EXISTS ocr_confidence,
              DROP COLUMN IF EXISTS ocr_method
        SQL);
    }
};
