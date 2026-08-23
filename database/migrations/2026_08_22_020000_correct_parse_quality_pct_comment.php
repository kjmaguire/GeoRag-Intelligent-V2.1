<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Comment-only. `parse_quality_pct` does not measure parse quality.
 *
 * It is `unique_section_numbers / 17` — how much of the NI 43-101 section
 * skeleton a document exhibits. That is a useful number under a name that
 * promises a different one, and it carries 50% of the weight in
 * `extraction_confidence`, so the misreading propagates.
 *
 * The two failure directions are both real and both silent:
 *
 *   A 300-page 1970s government geophysics survey extracted flawlessly —
 *   every page clean text through Document Intelligence — has no NI 43-101
 *   numbering, scores 0.0, and reads as a failed parse.
 *
 *   A 500-page report whose table of contents yielded 17 numbered headings
 *   while 300 of its pages OCR'd to nothing scores 1.0.
 *
 * The column is NOT renamed here. `ni43101_section_coverage_pct` is the
 * honest name, but renaming means this migration plus every reader plus the
 * FastAPI insert, and a rename that lands without its readers is worse than
 * a wrong name. The comment is what stops a dashboard author being misled by
 * the schema in the meantime.
 *
 * pdf_report.py now also computes and LOGS `text_page_coverage`
 * (pages_with_text / page_count) beside this number, and warns when section
 * coverage is high while more than half the pages produced nothing — the
 * combination that looks healthiest and is worst. Promoting that into a
 * column is the follow-up this comment is holding the place for.
 */
return new class extends Migration
{
    private const COMMENT = 'MISNAMED. Measures NI 43-101 section-heading coverage (unique numbered sections / 17), NOT extraction completeness. A perfectly-extracted document with no NI 43-101 numbering scores 0.0; a document whose TOC yielded 17 headings while most pages OCR\'d to nothing scores 1.0. Feeds extraction_confidence at 50% weight. For extraction completeness see the text_page_coverage figure logged by pdf_report (pages_with_text / page_count) — not yet stored. Rename to ni43101_section_coverage_pct is pending its readers.';

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(sprintf(
            'COMMENT ON COLUMN silver.reports.parse_quality_pct IS %s',
            DB::getPdo()->quote(self::COMMENT),
        ));
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('COMMENT ON COLUMN silver.reports.parse_quality_pct IS NULL');
    }
};
