<?php

declare(strict_types=1);

namespace Database\Seeders\PublicGeoscience;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * Seed the `public_geo.commodity_aliases` crosswalk.
 *
 * Hardcoded set derived from:
 *   1. SMDI `GROUPING` domain values (plan §03c).
 *   2. SMDI `PRIMARYCOMMODITIES` + `ASSOCIATEDCOMMODITIES` common strings.
 *   3. International chemical-symbol / common-name pairs for metals
 *      routinely encountered in Canadian exploration datasets.
 *
 * The seeder is idempotent via UNIQUE(alias_lower). Running again updates
 * `canonical_*` fields for existing aliases rather than duplicating rows.
 *
 * Downstream: Silver-tier ingestion (Phase 2.2) looks up each raw commodity
 * string via LOWER(alias) → canonical_code + commodity_grouping. Unmatched
 * strings are logged and the record's commodity_grouping is set to NULL
 * (plan §04c).
 */
class CommodityAliasesSeeder extends Seeder
{
    /**
     * Rows are laid out as:
     *   [alias, canonical_code, canonical_name, commodity_grouping]
     *
     * Multiple alias rows may point to the same canonical code; that's
     * intended (e.g. "Au", "Gold", "gold (primary)" all → Au / gold).
     */
    private const ROWS = [
        // ── Precious metals ──────────────────────────────────────────
        ['Au',                 'Au',  'Gold',          'precious_metals'],
        ['Gold',               'Au',  'Gold',          'precious_metals'],
        ['AU',                 'Au',  'Gold',          'precious_metals'],
        ['Ag',                 'Ag',  'Silver',        'precious_metals'],
        ['Silver',             'Ag',  'Silver',        'precious_metals'],
        ['Pt',                 'Pt',  'Platinum',      'precious_metals'],
        ['Platinum',           'Pt',  'Platinum',      'precious_metals'],
        ['Pd',                 'Pd',  'Palladium',     'precious_metals'],
        ['Palladium',          'Pd',  'Palladium',     'precious_metals'],
        ['PGE',                'PGE', 'Platinum Group Elements', 'precious_metals'],
        ['PGM',                'PGE', 'Platinum Group Metals',   'precious_metals'],

        // ── Base metals ──────────────────────────────────────────────
        ['Cu',                 'Cu',  'Copper',        'base_metals'],
        ['Copper',             'Cu',  'Copper',        'base_metals'],
        ['Zn',                 'Zn',  'Zinc',          'base_metals'],
        ['Zinc',               'Zn',  'Zinc',          'base_metals'],
        ['Pb',                 'Pb',  'Lead',          'base_metals'],
        ['Lead',               'Pb',  'Lead',          'base_metals'],
        ['Ni',                 'Ni',  'Nickel',        'base_metals'],
        ['Nickel',             'Ni',  'Nickel',        'base_metals'],
        ['Co',                 'Co',  'Cobalt',        'base_metals'],
        ['Cobalt',             'Co',  'Cobalt',        'base_metals'],
        ['Mo',                 'Mo',  'Molybdenum',    'base_metals'],
        ['Molybdenum',         'Mo',  'Molybdenum',    'base_metals'],
        ['Sn',                 'Sn',  'Tin',           'base_metals'],
        ['Tin',                'Sn',  'Tin',           'base_metals'],
        ['Fe',                 'Fe',  'Iron',          'base_metals'],
        ['Iron',               'Fe',  'Iron',          'base_metals'],
        ['W',                  'W',   'Tungsten',      'base_metals'],
        ['Tungsten',           'W',   'Tungsten',      'base_metals'],
        ['Sb',                 'Sb',  'Antimony',      'base_metals'],
        ['Antimony',           'Sb',  'Antimony',      'base_metals'],
        ['Bi',                 'Bi',  'Bismuth',       'base_metals'],
        ['Bismuth',            'Bi',  'Bismuth',       'base_metals'],
        ['Base Metals',        'BM',  'Base Metals',   'base_metals'],

        // ── Uranium / radioactive ────────────────────────────────────
        ['U',                  'U',   'Uranium',       'uranium'],
        ['U3O8',               'U',   'Uranium Oxide', 'uranium'],
        ['Uranium',            'U',   'Uranium',       'uranium'],
        ['Th',                 'Th',  'Thorium',       'uranium'],
        ['Thorium',            'Th',  'Thorium',       'uranium'],

        // ── Potash & salt ────────────────────────────────────────────
        ['K2O',                'K',   'Potash',        'potash_salt'],
        ['Potash',             'K',   'Potash',        'potash_salt'],
        ['KCl',                'K',   'Potash',        'potash_salt'],
        ['Halite',             'NaCl', 'Salt',         'potash_salt'],
        ['Salt',               'NaCl', 'Salt',         'potash_salt'],
        ['NaCl',               'NaCl', 'Salt',         'potash_salt'],
        ['Potash-Salt',        'K',   'Potash/Salt',   'potash_salt'],

        // ── Industrial materials ─────────────────────────────────────
        ['Industrial Materials', 'IM', 'Industrial Materials', 'industrial_materials'],
        ['Industrial Minerals',  'IM', 'Industrial Minerals',  'industrial_materials'],
        ['Gypsum',             'Gypsum',    'Gypsum',       'industrial_materials'],
        ['Barite',             'Barite',    'Barite',       'industrial_materials'],
        ['Fluorite',            'Fluorite',  'Fluorite',     'industrial_materials'],
        ['Graphite',            'Graphite',  'Graphite',     'industrial_materials'],
        ['Peat',                'Peat',      'Peat',         'industrial_materials'],
        ['Silica',              'Silica',    'Silica',       'industrial_materials'],
        ['Helium',              'He',        'Helium',       'industrial_materials'],
        ['He',                  'He',        'Helium',       'industrial_materials'],
        ['Bitumen',             'Bitumen',   'Bitumen',      'industrial_materials'],
        ['Limestone',           'Limestone', 'Limestone',    'industrial_materials'],
        ['Dolomite',            'Dolomite',  'Dolomite',     'industrial_materials'],

        // ── Gemstones ────────────────────────────────────────────────
        ['Diamond',             'Diamond',   'Diamond',      'gemstones'],
        ['Gemstones',           'Gemstones', 'Gemstones',    'gemstones'],

        // ── Lithium ──────────────────────────────────────────────────
        ['Li',                  'Li',  'Lithium',        'lithium'],
        ['Lithium',             'Li',  'Lithium',        'lithium'],
        ['Li2O',                'Li',  'Lithium Oxide',  'lithium'],

        // ── REE ──────────────────────────────────────────────────────
        ['REE',                 'REE', 'Rare Earth Elements',  'ree'],
        ['Rare Earth Elements', 'REE', 'Rare Earth Elements',  'ree'],
        ['Rare Earths',         'REE', 'Rare Earth Elements',  'ree'],
        ['Ce',                  'Ce',  'Cerium',              'ree'],
        ['La',                  'La',  'Lanthanum',           'ree'],
        ['Nd',                  'Nd',  'Neodymium',           'ree'],
        ['Y',                   'Y',   'Yttrium',             'ree'],
        ['Sc',                  'Sc',  'Scandium',            'ree'],
        ['Scandium',            'Sc',  'Scandium',            'ree'],

        // ── Coal ─────────────────────────────────────────────────────
        ['Coal',                'Coal', 'Coal',           'coal'],
        ['Lignite',             'Coal', 'Lignite',        'coal'],
        ['Anthracite',          'Coal', 'Anthracite',     'coal'],

        // ── Other / catch-alls ───────────────────────────────────────
        ['Other',               'Other', 'Other',         'other'],
        ['Unknown',             'Unknown', 'Unknown',     'other'],
    ];

    public function run(): void
    {
        $now = now();

        foreach (self::ROWS as [$alias, $code, $name, $grouping]) {
            DB::table('public_geo.commodity_aliases')->updateOrInsert(
                ['alias_lower' => mb_strtolower($alias)],
                [
                    'alias' => $alias,
                    'canonical_code' => $code,
                    'canonical_name' => $name,
                    'commodity_grouping' => $grouping,
                    'updated_at' => $now,
                    'created_at' => $now,
                ],
            );
        }

        $this->command?->info('Seeded '.count(self::ROWS).' commodity aliases.');
    }
}
