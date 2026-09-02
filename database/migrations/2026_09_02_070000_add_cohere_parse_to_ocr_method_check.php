<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Allow ocr_method = 'cohere_parse'.
 *
 * Cohere Parse v5 (Azure AI Foundry) replaces Azure Document Intelligence as
 * the primary OCR engine on 2026-09-02 (ADR-0019). The CHECK on
 * silver.document_passages.ocr_method is the only machine-readable statement
 * of which engines the pipeline has, so the new label must be admitted here
 * BEFORE the image that writes it rolls — CD runs `artisan migrate` first.
 *
 * 'document_intelligence' stays in the set: rows written between 2026-07-29
 * and the cutover still carry it, and the label is historical provenance, not
 * a live engine. A later non-fatal tighten (see
 * 2026_08_20_070000_drop_docling_from_ocr_method_check.php for the pattern)
 * can retire it once no row carries it.
 */
return new class extends Migration
{
    /** Engines that can still be produced, plus historical labels on live rows. */
    private const VALUES = "'fitz_native', 'pdfplumber_native', 'tesseract', 'document_intelligence', 'cohere_parse', 'unavailable'";

    private const PREVIOUS = "'fitz_native', 'pdfplumber_native', 'tesseract', 'document_intelligence', 'unavailable'";

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return; // sqlite test DB carries no CHECK for this column
        }

        $this->replaceCheck(self::VALUES);

        // The column comments are part of the §04e contract and both said
        // NULL confidence meant "text layer". Cohere Parse reports no
        // confidence, so NULL now means "no engine confidence"; ocr_method
        // is the discriminator.
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.ocr_confidence IS
              'Per-passage OCR engine confidence, 0.0–1.0, from engines that measure one (tesseract). NULL means no engine confidence exists: the passage came from the PDF text layer, or from cohere_parse, which reports none (ADR-0019). ocr_method is the discriminator. Travels with the qdrant payload so retrieval can weight low-confidence chunks down.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.ocr_method IS
              'Which engine produced the text: fitz_native, pdfplumber_native, tesseract, cohere_parse (ADR-0019), document_intelligence (historical rows), or unavailable. NULL when extraction predates Phase 3.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $this->replaceCheck(self::PREVIOUS);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.ocr_confidence IS
              'Phase 3 — per-passage OCR engine confidence, 0.0–1.0. NULL means the passage came from the PDF text layer (no OCR involved). Travels with the qdrant payload so retrieval can weight low-confidence chunks down.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.ocr_method IS
              'Phase 3 — which engine produced the text: fitz_native, pdfplumber_native, docling_rapidocr, or tesseract. NULL when extraction predates Phase 3.'
        SQL);
    }

    private function replaceCheck(string $quotedValues): void
    {
        DB::statement('ALTER TABLE silver.document_passages DROP CONSTRAINT IF EXISTS document_passages_ocr_method_check');
        DB::statement(
            'ALTER TABLE silver.document_passages ADD CONSTRAINT document_passages_ocr_method_check '
            .'CHECK (ocr_method IS NULL OR ocr_method IN ('.$quotedValues.'))',
        );
    }
};
