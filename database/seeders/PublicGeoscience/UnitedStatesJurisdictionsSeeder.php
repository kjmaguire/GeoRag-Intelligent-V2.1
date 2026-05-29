<?php

declare(strict_types=1);

namespace Database\Seeders\PublicGeoscience;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * CC-02 Item 5 — first non-Canadian jurisdictions.
 *
 * Seeds 6 US jurisdiction rows + 1 federal (USGS MRDS) data-source row.
 *
 * Status defaults to 'coming_soon' for the state entries; only US-FED
 * is 'active' since USGS MRDS is the immediate target adapter
 * (src/fastapi/app/services/publicgeo/usgs_mrds_adapter.py).
 *
 * Per-state adapters can flip 'coming_soon' → 'active' as they land.
 * The recommended next state is Nevada (US-NV) because it has the
 * highest density of active exploration; Arizona (US-AZ) and Alaska
 * (US-AK) are next-best on similar density grounds.
 *
 * Idempotent: re-runs update fields without duplicating rows
 * (jurisdiction_code is the unique key).
 *
 * Usage:
 *   php artisan db:seed --class='Database\Seeders\PublicGeoscience\UnitedStatesJurisdictionsSeeder'
 */
class UnitedStatesJurisdictionsSeeder extends Seeder
{
    public function run(): void
    {
        $now = now();

        $jurisdictions = [
            [
                'jurisdiction_code' => 'US-FED',
                'country_code' => 'US',
                'display_name' => 'United States (federal)',
                'level' => 'federal',
                'status' => 'active',
                'primary_authority' => 'United States Geological Survey (USGS)',
                'license_summary' => 'USGS data is in the U.S. public domain (17 USC § 105).',
                'license_url' => 'https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits',
                'default_source_crs' => 4326, // USGS MRDS publishes in WGS84
                'refresh_cadence' => 'monthly',
                'teaser' => 'USGS MRDS — national mineral resources database, all 50 states + territories',
                'sort_order' => 200,
                'bbox_wkt' => 'POLYGON((-179.15 18.91, -66.93 18.91, -66.93 71.39, -179.15 71.39, -179.15 18.91))',
            ],
            [
                'jurisdiction_code' => 'US-NV',
                'country_code' => 'US',
                'display_name' => 'Nevada',
                'level' => 'state',
                'status' => 'coming_soon',
                'primary_authority' => 'Nevada Bureau of Mines and Geology (NBMG)',
                'teaser' => 'NBMG mineral occurrence + active claim density — densest US exploration jurisdiction',
                'sort_order' => 210,
                'bbox_wkt' => 'POLYGON((-120.01 35.00, -114.04 35.00, -114.04 42.00, -120.01 42.00, -120.01 35.00))',
            ],
            [
                'jurisdiction_code' => 'US-AZ',
                'country_code' => 'US',
                'display_name' => 'Arizona',
                'level' => 'state',
                'status' => 'coming_soon',
                'primary_authority' => 'Arizona Geological Survey (AZGS)',
                'teaser' => 'AZGS mineral inventory — major Cu / Mo / Au porphyry trend',
                'sort_order' => 220,
            ],
            [
                'jurisdiction_code' => 'US-AK',
                'country_code' => 'US',
                'display_name' => 'Alaska',
                'level' => 'state',
                'status' => 'coming_soon',
                'primary_authority' => 'Alaska Division of Geological & Geophysical Surveys (DGGS)',
                'teaser' => 'Alaska DGGS — frontier exploration, large undigitized data piles',
                'sort_order' => 230,
            ],
            [
                'jurisdiction_code' => 'US-CO',
                'country_code' => 'US',
                'display_name' => 'Colorado',
                'level' => 'state',
                'status' => 'coming_soon',
                'primary_authority' => 'Colorado Geological Survey',
                'teaser' => 'CGS mineral resources — Au / Mo / U / REE',
                'sort_order' => 240,
            ],
            [
                'jurisdiction_code' => 'US-CA',
                'country_code' => 'US',
                'display_name' => 'California',
                'level' => 'state',
                'status' => 'coming_soon',
                'primary_authority' => 'California Geological Survey',
                'teaser' => 'CGS mineral land classification — mostly legacy + industrial minerals',
                'sort_order' => 250,
            ],
        ];

        foreach ($jurisdictions as $row) {
            $bboxWkt = $row['bbox_wkt'] ?? null;
            unset($row['bbox_wkt']);

            $row['updated_at'] = $now;

            DB::table('public_geo.jurisdictions')
                ->updateOrInsert(
                    ['jurisdiction_code' => $row['jurisdiction_code']],
                    array_merge($row, ['created_at' => $now]),
                );

            if ($bboxWkt !== null) {
                DB::statement(
                    'UPDATE public_geo.jurisdictions
                        SET bbox = ST_GeomFromText(?, 4326)
                      WHERE jurisdiction_code = ?',
                    [$bboxWkt, $row['jurisdiction_code']],
                );
            }
        }

        // ── USGS MRDS source (the active data feed for US-FED) ───────
        DB::table('public_geo.sources')->updateOrInsert(
            ['source_id' => 'usgs_mrds'],
            [
                'source_id' => 'usgs_mrds',
                'jurisdiction_code' => 'US-FED',
                'name' => 'USGS Mineral Resources Data System (MRDS)',
                'canonical_type' => 'mine',
                // MRDS is distributed as a static CSV / SQLite download
                // rather than an ArcGIS REST service. The skeleton
                // adapter at src/fastapi/app/services/publicgeo/
                // usgs_mrds_adapter.py uses a synthetic-stub feed today;
                // see https://mrdata.usgs.gov/mrds/ for the canonical
                // download URL and field reference.
                'service_url' => 'https://mrdata.usgs.gov/mrds/',
                'layer_index' => null,
                'source_crs' => 4326,
                'license_summary' => 'U.S. public domain (17 USC § 105) — USGS scientific data',
                'license_url' => 'https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits',
                'refresh_cadence' => 'monthly',
                'notes' => 'CC-02 Item 5 placeholder. Adapter uses synthetic-stub feed of ~8 major US mines pending switch to the real MRDS CSV download path.',
                'created_at' => $now,
                'updated_at' => $now,
            ],
        );

        $this->command?->info('Seeded 6 US jurisdictions (1 active = US-FED) + USGS MRDS source.');
    }
}
