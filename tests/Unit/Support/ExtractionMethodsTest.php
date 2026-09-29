<?php

declare(strict_types=1);

namespace Tests\Unit\Support;

use App\Support\ExtractionMethods;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

/**
 * Pages used to show the stored parser values raw — `fitz`, a leftover name
 * for PyMuPDF, which was removed in May — and to call Cohere Parse
 * "(Foundry)". These pin the names users now see.
 */
final class ExtractionMethodsTest extends TestCase
{
    /**
     * @return array<string, array{0: ?string, 1: ?string}>
     */
    public static function parsers(): array
    {
        return [
            'native text layer' => ['fitz', 'Native text (pypdfium2)'],
            'pdfplumber fallback' => ['pdfplumber', 'Native text (pdfplumber)'],
            'parse OCR' => ['ocr_cohere_parse', 'Cohere Parse OCR'],
            'tesseract OCR' => ['ocr_tesseract', 'Tesseract OCR'],
            'mixed OCR' => ['ocr_mixed', 'OCR (Cohere Parse + Tesseract)'],
            'unknown value passes through' => ['some-new-parser', 'some-new-parser'],
            'empty is null' => ['', null],
            'null is null' => [null, null],
        ];
    }

    #[DataProvider('parsers')]
    public function test_parser_label(?string $stored, ?string $expected): void
    {
        $this->assertSame($expected, ExtractionMethods::parserLabel($stored));
    }

    /**
     * @return array<string, array{0: ?string, 1: string}>
     */
    public static function pageMethods(): array
    {
        return [
            'native' => ['fitz_native', 'Native text (pypdfium2)'],
            'parse is not on Foundry' => ['cohere_parse', 'Cohere Parse'],
            'retired engine is marked retired' => ['document_intelligence', 'Azure Document Intelligence (retired)'],
            'null is not recorded' => [null, 'Not recorded'],
            'unknown value passes through' => ['some_engine', 'some_engine'],
        ];
    }

    #[DataProvider('pageMethods')]
    public function test_page_method_label(?string $stored, string $expected): void
    {
        $this->assertSame($expected, ExtractionMethods::pageMethodLabel($stored));
    }

    public function test_no_label_names_a_retired_library(): void
    {
        foreach (['fitz', 'pdfplumber', 'ocr_cohere_parse', 'ocr_mixed', 'unknown'] as $stored) {
            $label = (string) ExtractionMethods::parserLabel($stored);
            $this->assertStringNotContainsStringIgnoringCase('pymupdf', $label);
            $this->assertStringNotContainsStringIgnoringCase('fitz', $label);
            $this->assertStringNotContainsStringIgnoringCase('foundry', $label);
        }
        foreach (['fitz_native', 'pdfplumber_native', 'cohere_parse', 'tesseract'] as $stored) {
            $label = ExtractionMethods::pageMethodLabel($stored);
            $this->assertStringNotContainsStringIgnoringCase('fitz', $label);
            $this->assertStringNotContainsStringIgnoringCase('foundry', $label);
        }
    }
}
