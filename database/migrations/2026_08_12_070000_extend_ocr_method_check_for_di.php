<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * The ocr_method CHECK was frozen at the four pre-DI engines. Azure
 * Document Intelligence became the primary OCR (2026-07-29) and the
 * truth-labels fix (760f50c) added 'unavailable' for pages where no
 * engine could run — but no DI/unavailable-labelled passage had ever
 * reached the table until scanned-table support (f33e21e) produced one,
 * and persist failed on the constraint. Extends the set.
 */
return new class extends Migration
{
    private const VALUES = "'fitz_native', 'pdfplumber_native', 'docling_rapidocr', 'tesseract', 'document_intelligence', 'unavailable'";

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return; // sqlite test DB carries no CHECK for this column
        }

        DB::statement('ALTER TABLE silver.document_passages DROP CONSTRAINT IF EXISTS document_passages_ocr_method_check');
        DB::statement(
            'ALTER TABLE silver.document_passages ADD CONSTRAINT document_passages_ocr_method_check '
            .'CHECK (ocr_method IS NULL OR ocr_method IN ('.self::VALUES.'))',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.document_passages DROP CONSTRAINT IF EXISTS document_passages_ocr_method_check');
        DB::statement(
            'ALTER TABLE silver.document_passages ADD CONSTRAINT document_passages_ocr_method_check '
            ."CHECK (ocr_method IS NULL OR ocr_method IN ('fitz_native', 'pdfplumber_native', 'docling_rapidocr', 'tesseract'))",
        );
    }
};
