<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.assays_v2.qaqc_flag: NULL means "not evaluated", not 'pass'.
 *
 * The column was created as `text DEFAULT 'pass'` and ingest_tabular's INSERT
 * never named it, so every assay row ever written said 'pass' - and
 * nl_summaries rendered that as "QA/QC: pass" in the passage the agent
 * retrieves, and qaqc_availability counted the column as a populated
 * Silver Review classification. Nothing at ingest evaluates a blank, a
 * standard or a duplicate, so the claim was nobody's.
 *
 * ingest_tabular now sets the column explicitly: NULL, or `control_blank` /
 * `control_standard` / `control_duplicate` for the rows of a QA/QC control
 * sample. This migration removes the default so a writer that forgets the
 * column (a future one, a manual INSERT) gets NULL rather than a fabricated
 * pass.
 *
 * Existing rows are NOT touched: a 'pass' written by the old default cannot be
 * told from one a reviewer set, and rewriting either would be the guess this
 * change removes. A re-upload of the same file replaces its rows (and so
 * their flag) the way it replaces its values.
 *
 * The column carries no CHECK constraint and no vocabulary is defined
 * anywhere; the comment below records the values the ingest writer uses so
 * the SME can confirm or replace them (see CsvAssaysExporter).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement("SET LOCAL lock_timeout = '15s'");
        DB::statement('ALTER TABLE silver.assays_v2 ALTER COLUMN qaqc_flag DROP DEFAULT');
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.assays_v2.qaqc_flag IS
              'NULL = not evaluated. control_blank / control_standard / control_duplicate = the row belongs to a QA/QC control sample (written by ingest_tabular; says what the sample is, not whether it passed). Any other value was set by a review. No CHECK; vocabulary awaiting SME confirmation.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement("SET LOCAL lock_timeout = '15s'");
        DB::statement("ALTER TABLE silver.assays_v2 ALTER COLUMN qaqc_flag SET DEFAULT 'pass'");
        DB::statement('COMMENT ON COLUMN silver.assays_v2.qaqc_flag IS NULL');
    }
};
