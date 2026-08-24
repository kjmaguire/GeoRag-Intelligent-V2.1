<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drop 'docling_rapidocr' from the ocr_method CHECK.
 *
 * Docling was removed from the image on 2026-07-29 (see the removal notes
 * in src/fastapi/pyproject.toml), so nothing has been able to write this
 * label for weeks. Leaving it in the allowed set is not harmless: the
 * constraint is the only machine-readable statement of which OCR engines
 * this pipeline actually has, and a stale entry means a typo'd or
 * resurrected label passes validation instead of failing loudly.
 *
 * DELIBERATELY NON-FATAL. CD runs `artisan migrate` BEFORE the new image
 * deploys, so a migration that throws ships no code at all (see the
 * 2026-08-19 GRANT incident). Tightening a CHECK is exactly the kind of
 * statement that fails on data nobody predicted, so this counts first and
 * leaves the value in place if any row still carries it. Re-running the
 * migration after those rows are cleaned up completes the tightening.
 *
 * NOT addressed here: 'fitz_native' is also a lie — PyMuPDF was removed
 * 2026-05-27 over its AGPL licence and the real engines are pypdfium2 +
 * pdfminer.six. That label is on the large majority of rows, so renaming
 * it is a data migration with consumers to update, not hygiene.
 */
return new class extends Migration
{
    /** Engines that can still be produced by the live pipeline. */
    private const CURRENT = [
        'fitz_native',
        'pdfplumber_native',
        'tesseract',
        'document_intelligence',
        'unavailable',
    ];

    private const RETIRED = 'docling_rapidocr';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return; // sqlite test DB carries no CHECK for this column
        }

        $values = self::CURRENT;

        $stragglers = (int) DB::table('silver.document_passages')
            ->where('ocr_method', self::RETIRED)
            ->count();

        if ($stragglers > 0) {
            $values[] = self::RETIRED;
            logger()->warning(
                'drop_docling_from_ocr_method_check: leaving the retired label '
                .'in the CHECK because rows still carry it',
                ['ocr_method' => self::RETIRED, 'rows' => $stragglers],
            );
        }

        $this->replaceCheck($values);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $this->replaceCheck([...self::CURRENT, self::RETIRED]);
    }

    /**
     * @param list<string> $values
     */
    private function replaceCheck(array $values): void
    {
        $quoted = implode(', ', array_map(
            static fn (string $value): string => "'".$value."'",
            $values,
        ));

        DB::statement('ALTER TABLE silver.document_passages DROP CONSTRAINT IF EXISTS document_passages_ocr_method_check');
        DB::statement(
            'ALTER TABLE silver.document_passages ADD CONSTRAINT document_passages_ocr_method_check '
            .'CHECK (ocr_method IS NULL OR ocr_method IN ('.$quoted.'))',
        );
    }
};
