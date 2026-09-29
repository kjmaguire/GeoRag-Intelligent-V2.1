<?php

declare(strict_types=1);

namespace App\Support;

/**
 * Display names for the tools that extracted a document, in one place.
 *
 * Two stored values say which tool ran, and neither is what a user should
 * read:
 *
 *   - silver.reports.parser_used — the document's BASE parser. `fitz` means
 *     "the native text layer was read"; it is a historical name. PyMuPDF
 *     (imported as `fitz`) was removed for its AGPL licence on 2026-05-27,
 *     and that path has been pypdfium2 (PDFium) ever since. The value was
 *     kept because pdf_report.py gates on it.
 *   - silver.document_passages.ocr_method — the per-PAGE engine, the real
 *     answer to "was this OCR'd, and by what".
 *
 * Pages showed the raw values (`fitz`, `fitz_native`) and called Cohere
 * Parse "(Foundry)", a host it left under ADR-0023. Every page that names a
 * tool now reads it from here, so the names cannot drift apart again.
 *
 * The stored values themselves are unchanged: they are contract values
 * other code matches on. Unknown values pass through as-is rather than
 * being guessed at.
 */
final class ExtractionMethods
{
    /**
     * silver.reports.parser_used → display name.
     *
     * @var array<string, string>
     */
    private const PARSERS = [
        'fitz' => 'Native text (pypdfium2)',
        'pdfplumber' => 'Native text (pdfplumber)',
        'ocr_cohere_parse' => 'Cohere Parse OCR',
        'ocr_tesseract' => 'Tesseract OCR',
        'ocr_mixed' => 'OCR (Cohere Parse + Tesseract)',
        'openpyxl' => 'Excel (openpyxl)',
        'xlrd' => 'Excel 97-2003 (xlrd)',
        'csv-text' => 'CSV / text',
        'derived-from-las-curves-v1' => 'Derived from LAS curves',
        'skipped' => 'Skipped',
        'unknown' => 'Not recorded',
    ];

    /**
     * silver.document_passages.ocr_method → display name.
     *
     * @var array<string, string>
     */
    private const PAGE_METHODS = [
        'fitz_native' => 'Native text (pypdfium2)',
        'pdfplumber_native' => 'Native text (pdfplumber)',
        'cohere_parse' => 'Cohere Parse',
        'tesseract' => 'Tesseract OCR',
        // Retired 2026-09-02 (ADR-0019); passages ingested before then keep it.
        'document_intelligence' => 'Azure Document Intelligence (retired)',
        'unavailable' => 'No OCR engine available',
        'unknown' => 'Not recorded',
    ];

    /**
     * Page methods that read a PDF's own text layer — no OCR ran.
     *
     * @var list<string>
     */
    public const NATIVE_PAGE_METHODS = ['fitz_native', 'pdfplumber_native'];

    public static function parserLabel(?string $parserUsed): ?string
    {
        if ($parserUsed === null || $parserUsed === '') {
            return null;
        }

        return self::PARSERS[$parserUsed] ?? $parserUsed;
    }

    public static function pageMethodLabel(?string $ocrMethod): string
    {
        $method = ($ocrMethod === null || $ocrMethod === '') ? 'unknown' : $ocrMethod;

        return self::PAGE_METHODS[$method] ?? $method;
    }
}
