<?php

declare(strict_types=1);

namespace Database\Seeders\VendorProfiles;

use App\Models\ColumnMapping;
use App\Models\VendorProfile;
use Illuminate\Database\Seeder;

/**
 * CC-02 Item 6 — placeholder vendor profile for MX Deposit exports.
 *
 * Anna's team uses MX Deposit for active drill logging. We don't yet
 * have a real export file to reverse-engineer the schema from, so this
 * seeder lays down the profile shell and a starter set of column
 * mappings derived from MX Deposit's documented field names. Replace
 * with real mappings once Anna provides a sample.
 *
 * The mappings cover the four parser_type values most likely to apply
 * to MX Deposit exports:
 *   - csv_collar   (collar table)
 *   - csv_survey   (downhole surveys)
 *   - csv_lithology (lithology logs)
 *   - csv_sample   (sample submission / point samples)
 *
 * Each row is marked with `notes => 'PLACEHOLDER — verify against real
 * MX Deposit export'` so a future operator can find them with one grep
 * and replace them in bulk.
 *
 * Idempotent: re-running on an already-seeded database updates the
 * mappings in place without creating duplicates (per the (vendor_profile_id,
 * parser_type, canonical_field) unique constraint).
 *
 * Usage:
 *   php artisan db:seed --class='Database\Seeders\VendorProfiles\MxDepositSeeder'
 */
class MxDepositSeeder extends Seeder
{
    private const PROFILE_NAME = 'MX Deposit';

    private const PLACEHOLDER_NOTE = 'PLACEHOLDER — verify against real MX Deposit export';

    public function run(): void
    {
        $profile = VendorProfile::updateOrCreate(
            ['name' => self::PROFILE_NAME],
            [
                'description' => (
                    'MX Deposit by Datamine — active drill logging tool used by some junior '
                    .'explorers. Column mappings below are placeholders pending a real sample '
                    .'file (CC-02 Item 6, 2026-05-23).'
                ),
                'profile_type' => 'other',
                'is_global' => true,
            ],
        );

        $mappings = [
            // ── csv_collar ──────────────────────────────────────────────
            ['parser_type' => 'csv_collar', 'canonical' => 'hole_id',     'source' => 'HoleID',           'source_unit' => null,  'target_unit' => null],
            ['parser_type' => 'csv_collar', 'canonical' => 'easting',     'source' => 'Easting',          'source_unit' => 'm',   'target_unit' => 'm'],
            ['parser_type' => 'csv_collar', 'canonical' => 'northing',    'source' => 'Northing',         'source_unit' => 'm',   'target_unit' => 'm'],
            ['parser_type' => 'csv_collar', 'canonical' => 'elevation',   'source' => 'Elevation',        'source_unit' => 'm',   'target_unit' => 'm'],
            ['parser_type' => 'csv_collar', 'canonical' => 'total_depth', 'source' => 'EOH',              'source_unit' => 'm',   'target_unit' => 'm'],
            ['parser_type' => 'csv_collar', 'canonical' => 'azimuth',     'source' => 'AzimuthCollar',    'source_unit' => 'deg', 'target_unit' => 'deg'],
            ['parser_type' => 'csv_collar', 'canonical' => 'dip',         'source' => 'DipCollar',        'source_unit' => 'deg', 'target_unit' => 'deg'],
            ['parser_type' => 'csv_collar', 'canonical' => 'drill_date',  'source' => 'StartDate',        'source_unit' => null,  'target_unit' => null],

            // ── csv_survey ──────────────────────────────────────────────
            ['parser_type' => 'csv_survey', 'canonical' => 'hole_id', 'source' => 'HoleID',  'source_unit' => null,  'target_unit' => null],
            ['parser_type' => 'csv_survey', 'canonical' => 'depth',   'source' => 'Depth',   'source_unit' => 'm',   'target_unit' => 'm'],
            ['parser_type' => 'csv_survey', 'canonical' => 'azimuth', 'source' => 'Azimuth', 'source_unit' => 'deg', 'target_unit' => 'deg'],
            ['parser_type' => 'csv_survey', 'canonical' => 'dip',     'source' => 'Dip',     'source_unit' => 'deg', 'target_unit' => 'deg'],

            // ── csv_lithology ───────────────────────────────────────────
            ['parser_type' => 'csv_lithology', 'canonical' => 'hole_id',                'source' => 'HoleID',         'source_unit' => null, 'target_unit' => null],
            ['parser_type' => 'csv_lithology', 'canonical' => 'from_depth',             'source' => 'DepthFrom',      'source_unit' => 'm',  'target_unit' => 'm'],
            ['parser_type' => 'csv_lithology', 'canonical' => 'to_depth',               'source' => 'DepthTo',        'source_unit' => 'm',  'target_unit' => 'm'],
            ['parser_type' => 'csv_lithology', 'canonical' => 'lithology_code',         'source' => 'LithCode',       'source_unit' => null, 'target_unit' => null],
            ['parser_type' => 'csv_lithology', 'canonical' => 'lithology_description',  'source' => 'LithComment',    'source_unit' => null, 'target_unit' => null],
            ['parser_type' => 'csv_lithology', 'canonical' => 'grain_size',             'source' => 'GrainSize',      'source_unit' => null, 'target_unit' => null],
            ['parser_type' => 'csv_lithology', 'canonical' => 'color',                  'source' => 'Colour',         'source_unit' => null, 'target_unit' => null],
            ['parser_type' => 'csv_lithology', 'canonical' => 'hardness',               'source' => 'Hardness',       'source_unit' => null, 'target_unit' => null],
            ['parser_type' => 'csv_lithology', 'canonical' => 'weathering',             'source' => 'Weathering',     'source_unit' => null, 'target_unit' => null],

            // ── csv_sample ──────────────────────────────────────────────
            ['parser_type' => 'csv_sample', 'canonical' => 'hole_id',     'source' => 'HoleID',     'source_unit' => null, 'target_unit' => null],
            ['parser_type' => 'csv_sample', 'canonical' => 'sample_id',   'source' => 'SampleID',   'source_unit' => null, 'target_unit' => null],
            ['parser_type' => 'csv_sample', 'canonical' => 'from_depth',  'source' => 'DepthFrom',  'source_unit' => 'm',  'target_unit' => 'm'],
            ['parser_type' => 'csv_sample', 'canonical' => 'to_depth',    'source' => 'DepthTo',    'source_unit' => 'm',  'target_unit' => 'm'],
            ['parser_type' => 'csv_sample', 'canonical' => 'sample_type', 'source' => 'SampleType', 'source_unit' => null, 'target_unit' => null],
        ];

        foreach ($mappings as $m) {
            ColumnMapping::updateOrCreate(
                [
                    'vendor_profile_id' => $profile->id,
                    'parser_type' => $m['parser_type'],
                    'canonical_field' => $m['canonical'],
                ],
                [
                    'source_column' => $m['source'],
                    'source_unit' => $m['source_unit'],
                    'target_unit' => $m['target_unit'],
                    'notes' => self::PLACEHOLDER_NOTE,
                ],
            );
        }

        $count = count($mappings);
        $this->command?->info(
            "Seeded MX Deposit vendor profile (id={$profile->id}) with {$count} placeholder column mappings.",
        );
    }
}
