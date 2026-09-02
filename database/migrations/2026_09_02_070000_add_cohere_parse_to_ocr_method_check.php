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
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $this->replaceCheck(self::PREVIOUS);
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
