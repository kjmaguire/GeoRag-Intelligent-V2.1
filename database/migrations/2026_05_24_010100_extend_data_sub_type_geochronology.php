<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-03 Item 3 — extend silver.data_sub_type with geochronology sub-types.
 *
 * The CC-04 taxonomy seed (migration 2026_05_23_040000) reserved 2xx for
 * the Geology domain but stopped at id 210 (logged_geotechnical). Slots
 * 211-217 are claimed here for the radiometric-age sub-types, one per
 * isotopic system in chk_geochron_isotopic_system. The
 * silver_geochronology_samples Dagster asset writes one
 * silver.document_domain_tag row per (source_file, isotopic_system) using
 * these IDs.
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
              (211, 2, 'geochronology_uranium_lead',      'Geochronology — U-Pb / Pb-Pb'),
              (212, 2, 'geochronology_argon_argon',       'Geochronology — Ar-Ar / K-Ar'),
              (213, 2, 'geochronology_rhenium_osmium',    'Geochronology — Re-Os'),
              (214, 2, 'geochronology_rubidium_strontium','Geochronology — Rb-Sr'),
              (215, 2, 'geochronology_samarium_neodymium','Geochronology — Sm-Nd'),
              (216, 2, 'geochronology_lutetium_hafnium',  'Geochronology — Lu-Hf'),
              (217, 2, 'geochronology_other',             'Geochronology — Other isotopic system')
            ON CONFLICT (id) DO NOTHING
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DELETE FROM silver.data_sub_type WHERE id BETWEEN 211 AND 217');
    }
};
