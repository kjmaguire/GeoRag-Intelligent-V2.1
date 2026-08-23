<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Store the extraction number, not just the one people mistake for it.
 *
 * This is the follow-up 2026_08_22_020000 was holding the place for. That
 * migration could only put a warning label on `parse_quality_pct`: it
 * measures NI 43-101 section-heading coverage, so a flawlessly-extracted
 * 1970s geophysics survey with no numbered sections scores 0.0, and a
 * report whose table of contents yielded 17 headings while 300 pages OCR'd
 * to nothing scores 1.0. A comment stops a dashboard author being misled by
 * the schema; it does not give anyone the number they actually wanted.
 *
 * `text_page_coverage_pct` is that number — pages_with_text / page_count,
 * already computed in pdf_report.py and, until now, only logged. Stored as
 * a fraction (0.0–1.0), matching parse_quality_pct's units so nobody has to
 * remember which of the two is a percentage.
 *
 * NULLABLE ON PURPOSE. Every existing row predates the column, and
 * backfilling would mean re-parsing the corpus. NULL means "not measured",
 * which is honestly different from 0.0 meaning "no page produced text" —
 * and 0.0 is exactly the value the OCR review queue should react to, so
 * conflating the two would manufacture a review backlog out of history.
 *
 * The rename of parse_quality_pct to ni43101_section_coverage_pct is still
 * pending. It is read by the Dagster assets, two Laravel controllers and
 * two React pages, so it is a data migration with consumers rather than
 * hygiene — and considerably less urgent now that the honest number sits
 * beside it.
 */
return new class extends Migration
{
    private const COMMENT = 'Fraction of the source PDF\'s pages that produced any text (pages_with_text / page_count), 0.0-1.0. THIS is extraction completeness; parse_quality_pct beside it is NI 43-101 section-heading coverage and answers a different question. NULL means not measured (row predates the column), which is deliberately distinct from 0.0 meaning no page produced text.';

    public function up(): void
    {
        // SQLite has no silver schema; the whole bronze/silver layer is
        // gated on driver=pgsql throughout this chain.
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // IF NOT EXISTS rather than a Schema::hasColumn guard: CD runs
        // `artisan migrate` BEFORE it rolls the images, so a migration that
        // throws ships no code at all (the 2026-08-19 GRANT incident). An
        // additive nullable column cannot fail on data, but it can fail on
        // having already been applied by hand.
        DB::statement(
            'ALTER TABLE silver.reports
                 ADD COLUMN IF NOT EXISTS text_page_coverage_pct real',
        );

        DB::statement(sprintf(
            'COMMENT ON COLUMN silver.reports.text_page_coverage_pct IS %s',
            DB::getPdo()->quote(self::COMMENT),
        ));

        // The FastAPI ingest tier writes this column. Grants on
        // silver.reports are already held by georag_app, but a column added
        // after the fact inherits the table grant only for privileges that
        // are table-wide — which UPDATE is, when granted without a column
        // list. Re-stating it is a no-op when that is already true and the
        // difference between a working ingest and a 500 when it is not.
        DB::statement('GRANT SELECT, INSERT, UPDATE ON silver.reports TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(
            'ALTER TABLE silver.reports DROP COLUMN IF EXISTS text_page_coverage_pct',
        );
    }
};
