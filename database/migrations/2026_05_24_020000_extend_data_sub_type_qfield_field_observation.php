<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-03 Item 4 — extend silver.data_sub_type with QField field observation.
 *
 * The CC-04 taxonomy seed (migration 2026_05_23_040000) seeded Geology
 * sub-types 201-210; CC-03 Item 3 (migration 2026_05_24_010100) added
 * 211-217 for geochronology. The original CC-03 Item 4 brief asked for
 * id=213 but that slot is already used by 'geochronology_rhenium_osmium'.
 * Next free slot in the Geology (2xx) range is 218.
 *
 * The QField .gpkg parser writes one silver.document_domain_tag row per
 * QField source file with (domain_id=2 geology, sub_type_id=218
 * field_observation).
 *
 * SQLite — gated on Postgres (sub_type ID space is irrelevant in test DB).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            INSERT INTO silver.data_sub_type (id, domain_id, code, label) VALUES
              (218, 2, 'field_observation', 'Field observation (QField)')
            ON CONFLICT (id) DO NOTHING
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DELETE FROM silver.data_sub_type WHERE id = 218');
    }
};
