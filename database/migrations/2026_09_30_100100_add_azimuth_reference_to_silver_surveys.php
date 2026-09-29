<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Per-file azimuth north reference on silver.surveys, and the documented
 * units of the project-level one (§04e — Kyle, 2026-09-29).
 *
 * silver.surveys.azimuth_reference — 'true' | 'magnetic' | 'grid', or NULL.
 *   Populated by ingest_tabular from an azimuth-reference column in the
 *   survey file (Azimuth_Ref / Az_Reference / North_Ref ...), canonicalised
 *   by georag_geoparsers._azimuth_reference. NULL = the file declared
 *   nothing. Desurvey (promote_silver_to_gold._promote_traces) uses a
 *   station's own value in preference to the project's
 *   orientation_reference, and applies no correction when neither declares
 *   one (Kyle's default). No backfill: existing stations were ingested from
 *   files whose reference, if any, was never read, and inventing one would
 *   be exactly the guess the default exists to avoid.
 *
 * silver.projects.magnetic_declination — NOT a new column. It has existed
 *   since 2026_04_09_180000 (float, nullable) and is what the azimuth
 *   correction already reads; a second `magnetic_declination_deg` would be
 *   two sources of truth for one number. This only records its convention:
 *   degrees, EAST positive, NULL = not recorded (which is not 0).
 *
 * silver.projects.orientation_reference — no CHECK (legacy 'grid_north'
 *   rows; see 2026_09_29_210400). Its accepted vocabulary widens in the
 *   Laravel requests to BOH / TOH / grid / true / magnetic; this records it.
 *
 * Catalog-only apart from the CHECK's validation scan of a column that is
 * NULL on every existing row.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.surveys ADD COLUMN IF NOT EXISTS azimuth_reference varchar(10)');
        DB::statement('ALTER TABLE silver.surveys DROP CONSTRAINT IF EXISTS chk_surveys_azimuth_reference');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.surveys
              ADD CONSTRAINT chk_surveys_azimuth_reference
              CHECK (azimuth_reference IS NULL OR azimuth_reference IN ('true', 'magnetic', 'grid'))
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.surveys.azimuth_reference IS
              'Which north this station''s azimuth is measured from, as DECLARED by the survey file (true | magnetic | grid). NULL = the file declared nothing. Preferred over silver.projects.orientation_reference at desurvey; magnetic uses silver.projects.magnetic_declination. 2026-09-29.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.projects.magnetic_declination IS
              'Magnetic declination in degrees, EAST positive (west negative). NULL = not recorded, which is not 0: a magnetic azimuth reference without it is not corrected.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.projects.orientation_reference IS
              'BOH | TOH (core-orientation mark; declares no azimuth north) or the project''s default azimuth north reference: grid | true | magnetic (2026-09-29). Legacy ''grid_north'' reads as grid.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.surveys DROP CONSTRAINT IF EXISTS chk_surveys_azimuth_reference');
        DB::statement('ALTER TABLE silver.surveys DROP COLUMN IF EXISTS azimuth_reference');
        DB::statement('COMMENT ON COLUMN silver.projects.magnetic_declination IS NULL');
        DB::statement('COMMENT ON COLUMN silver.projects.orientation_reference IS NULL');
    }
};
