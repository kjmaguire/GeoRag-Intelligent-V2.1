<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Seed public_geo.commodity_aliases and the base public_geo.status_aliases.
 *
 * Found during the 2026-08-20 Azure review. Azure holds 522,768 public_geo
 * rows, so the corpus is there — but:
 *
 *     commodity_aliases    0 rows on Azure, 77 locally
 *     status_aliases      23 rows on Azure, 62 locally
 *
 * The 23 are the ones the 2026-08-20 migration added. The other 39, and all
 * 77 commodity aliases, only ever existed in a seeder class
 * (database/seeders/PublicGeoscience/*) that is not part of the migration
 * chain — so they were applied to the local cluster by hand and Azure never
 * got them. Same recurring shape as silver.collars.geom_4326 and the
 * public_geo source registry: real configuration living outside the chain.
 *
 * Why it matters, concretely
 * -------------------------
 * These are not decoration. public_geo_sync resolves every upstream label
 * through them before writing:
 *
 *   - commodity_grouping is looked up in commodity_aliases. With the table
 *     empty, EVERY synced row on Azure gets commodity_grouping = NULL, so
 *     "show me the uranium properties" filters return nothing.
 *   - status is looked up in status_aliases, scoped by (jurisdiction,
 *     canonical_type). With only the 23 mine-status rows present, every BC
 *     MINFILE occurrence and most SMDI values resolve to 'unknown' — a
 *     legal value, which is exactly why it would have gone unnoticed.
 *
 * Content is transcribed from the local cluster, which is the only place the
 * seeder ever ran. Idempotent: ON CONFLICT DO NOTHING on each table's natural
 * key, so an environment that already has a row keeps its own version.
 */
return new class extends Migration
{
    /** @var list<array{0: string, 1: string, 2: string, 3: string}> alias, code, name, grouping */
    private const COMMODITY = [
        ['AU', 'Au', 'Gold', 'precious_metals'],
        ['Ag', 'Ag', 'Silver', 'precious_metals'],
        ['Anthracite', 'Coal', 'Anthracite', 'coal'],
        ['Antimony', 'Sb', 'Antimony', 'base_metals'],
        ['Barite', 'Barite', 'Barite', 'industrial_materials'],
        ['Base Metals', 'BM', 'Base Metals', 'base_metals'],
        ['Bi', 'Bi', 'Bismuth', 'base_metals'],
        ['Bismuth', 'Bi', 'Bismuth', 'base_metals'],
        ['Bitumen', 'Bitumen', 'Bitumen', 'industrial_materials'],
        ['Ce', 'Ce', 'Cerium', 'ree'],
        ['Co', 'Co', 'Cobalt', 'base_metals'],
        ['Coal', 'Coal', 'Coal', 'coal'],
        ['Cobalt', 'Co', 'Cobalt', 'base_metals'],
        ['Copper', 'Cu', 'Copper', 'base_metals'],
        ['Cu', 'Cu', 'Copper', 'base_metals'],
        ['Diamond', 'Diamond', 'Diamond', 'gemstones'],
        ['Dolomite', 'Dolomite', 'Dolomite', 'industrial_materials'],
        ['Fe', 'Fe', 'Iron', 'base_metals'],
        ['Fluorite', 'Fluorite', 'Fluorite', 'industrial_materials'],
        ['Gemstones', 'Gemstones', 'Gemstones', 'gemstones'],
        ['Gold', 'Au', 'Gold', 'precious_metals'],
        ['Graphite', 'Graphite', 'Graphite', 'industrial_materials'],
        ['Gypsum', 'Gypsum', 'Gypsum', 'industrial_materials'],
        ['Halite', 'NaCl', 'Salt', 'potash_salt'],
        ['He', 'He', 'Helium', 'industrial_materials'],
        ['Helium', 'He', 'Helium', 'industrial_materials'],
        ['Industrial Materials', 'IM', 'Industrial Materials', 'industrial_materials'],
        ['Industrial Minerals', 'IM', 'Industrial Minerals', 'industrial_materials'],
        ['Iron', 'Fe', 'Iron', 'base_metals'],
        ['K2O', 'K', 'Potash', 'potash_salt'],
        ['KCl', 'K', 'Potash', 'potash_salt'],
        ['La', 'La', 'Lanthanum', 'ree'],
        ['Lead', 'Pb', 'Lead', 'base_metals'],
        ['Li', 'Li', 'Lithium', 'lithium'],
        ['Li2O', 'Li', 'Lithium Oxide', 'lithium'],
        ['Lignite', 'Coal', 'Lignite', 'coal'],
        ['Limestone', 'Limestone', 'Limestone', 'industrial_materials'],
        ['Lithium', 'Li', 'Lithium', 'lithium'],
        ['Mo', 'Mo', 'Molybdenum', 'base_metals'],
        ['Molybdenum', 'Mo', 'Molybdenum', 'base_metals'],
        ['NaCl', 'NaCl', 'Salt', 'potash_salt'],
        ['Nd', 'Nd', 'Neodymium', 'ree'],
        ['Ni', 'Ni', 'Nickel', 'base_metals'],
        ['Nickel', 'Ni', 'Nickel', 'base_metals'],
        ['Other', 'Other', 'Other', 'other'],
        ['PGE', 'PGE', 'Platinum Group Elements', 'precious_metals'],
        ['PGM', 'PGE', 'Platinum Group Metals', 'precious_metals'],
        ['Palladium', 'Pd', 'Palladium', 'precious_metals'],
        ['Pb', 'Pb', 'Lead', 'base_metals'],
        ['Pd', 'Pd', 'Palladium', 'precious_metals'],
        ['Peat', 'Peat', 'Peat', 'industrial_materials'],
        ['Platinum', 'Pt', 'Platinum', 'precious_metals'],
        ['Potash', 'K', 'Potash', 'potash_salt'],
        ['Potash-Salt', 'K', 'Potash/Salt', 'potash_salt'],
        ['Pt', 'Pt', 'Platinum', 'precious_metals'],
        ['REE', 'REE', 'Rare Earth Elements', 'ree'],
        ['Rare Earth Elements', 'REE', 'Rare Earth Elements', 'ree'],
        ['Rare Earths', 'REE', 'Rare Earth Elements', 'ree'],
        ['Salt', 'NaCl', 'Salt', 'potash_salt'],
        ['Sb', 'Sb', 'Antimony', 'base_metals'],
        ['Sc', 'Sc', 'Scandium', 'ree'],
        ['Scandium', 'Sc', 'Scandium', 'ree'],
        ['Silica', 'Silica', 'Silica', 'industrial_materials'],
        ['Silver', 'Ag', 'Silver', 'precious_metals'],
        ['Sn', 'Sn', 'Tin', 'base_metals'],
        ['Th', 'Th', 'Thorium', 'uranium'],
        ['Thorium', 'Th', 'Thorium', 'uranium'],
        ['Tin', 'Sn', 'Tin', 'base_metals'],
        ['Tungsten', 'W', 'Tungsten', 'base_metals'],
        ['U', 'U', 'Uranium', 'uranium'],
        ['U3O8', 'U', 'Uranium Oxide', 'uranium'],
        ['Unknown', 'Unknown', 'Unknown', 'other'],
        ['Uranium', 'U', 'Uranium', 'uranium'],
        ['W', 'W', 'Tungsten', 'base_metals'],
        ['Y', 'Y', 'Yttrium', 'ree'],
        ['Zinc', 'Zn', 'Zinc', 'base_metals'],
        ['Zn', 'Zn', 'Zinc', 'base_metals'],
    ];

    /** @var list<array{0: string, 1: string, 2: string, 3: string}> jurisdiction, type, source_value, status */
    private const STATUS = [
        ['CA-BC', 'mineral_occurrence', 'Anomaly', 'occurrence'],
        ['CA-BC', 'mineral_occurrence', 'Developed Prospect', 'prospect'],
        ['CA-BC', 'mineral_occurrence', 'Occurrence', 'occurrence'],
        ['CA-BC', 'mineral_occurrence', 'Past Producer', 'past-producer'],
        ['CA-BC', 'mineral_occurrence', 'Producer', 'producer'],
        ['CA-BC', 'mineral_occurrence', 'Producing', 'producer'],
        ['CA-BC', 'mineral_occurrence', 'Prospect', 'prospect'],
        ['CA-BC', 'mineral_occurrence', 'Showing', 'showing'],
        ['CA-BC', 'mineral_occurrence', 'Unknown', 'unknown'],
        ['CA-SK', 'mineral_occurrence', 'Active Producer', 'producer'],
        ['CA-SK', 'mineral_occurrence', 'Advanced Prospect', 'prospect'],
        ['CA-SK', 'mineral_occurrence', 'Deposit', 'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Developed Deposit', 'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Former Producer', 'past-producer'],
        ['CA-SK', 'mineral_occurrence', 'Not Specified', 'unknown'],
        ['CA-SK', 'mineral_occurrence', 'Occurrence: Primary Exploration', 'occurrence'],
        ['CA-SK', 'mineral_occurrence', 'Occurrence', 'occurrence'],
        ['CA-SK', 'mineral_occurrence', 'Past Producer', 'past-producer'],
        ['CA-SK', 'mineral_occurrence', 'Past-Producer', 'past-producer'],
        ['CA-SK', 'mineral_occurrence', 'Producer', 'producer'],
        ['CA-SK', 'mineral_occurrence', 'Producing', 'producer'],
        ['CA-SK', 'mineral_occurrence', 'Prospect', 'prospect'],
        ['CA-SK', 'mineral_occurrence', 'Showing', 'showing'],
        ['CA-SK', 'mineral_occurrence', 'Unknown', 'unknown'],
        ['CA-SK', 'mine', 'Abandoned', 'closed'],
        ['CA-SK', 'mine', 'Active', 'producing'],
        ['CA-SK', 'mine', 'Advanced Prospect', 'prospect'],
        ['CA-SK', 'mine', 'Closed', 'closed'],
        ['CA-SK', 'mine', 'Developed Deposit', 'developed-deposit'],
        ['CA-SK', 'mine', 'Development', 'developed-deposit'],
        ['CA-SK', 'mine', 'Former Producer', 'past-producer'],
        ['CA-SK', 'mine', 'Not Specified', 'unknown'],
        ['CA-SK', 'mine', 'Past Producer', 'past-producer'],
        ['CA-SK', 'mine', 'Past-Producer', 'past-producer'],
        ['CA-SK', 'mine', 'Producer', 'producing'],
        ['CA-SK', 'mine', 'Producing', 'producing'],
        ['CA-SK', 'mine', 'Prospect', 'prospect'],
        ['CA-SK', 'mine', 'Reclaimed', 'closed'],
        ['CA-SK', 'mine', 'Unknown', 'unknown'],
    ];

    public function up(): void
    {
        if (! $this->ready()) {
            return;
        }

        foreach (self::COMMODITY as [$alias, $code, $name, $grouping]) {
            DB::statement(
                'INSERT INTO public_geo.commodity_aliases
                     (alias, alias_lower, canonical_code, canonical_name,
                      commodity_grouping, notes, created_at, updated_at)
                 VALUES (?, LOWER(?), ?, ?, ?, ?, NOW(), NOW())
                 ON CONFLICT (alias_lower) DO NOTHING',
                [$alias, $alias, $code, $name, $grouping, self::NOTE],
            );
        }

        foreach (self::STATUS as [$jurisdiction, $type, $sourceValue, $status]) {
            DB::statement(
                'INSERT INTO public_geo.status_aliases
                     (jurisdiction_code, canonical_type, source_value,
                      source_value_lower, canonical_status, notes,
                      created_at, updated_at)
                 VALUES (?, ?, ?, LOWER(?), ?, ?, NOW(), NOW())
                 ON CONFLICT (jurisdiction_code, canonical_type, source_value_lower)
                 DO NOTHING',
                [$jurisdiction, $type, $sourceValue, $sourceValue, $status, self::NOTE],
            );
        }
    }

    public function down(): void
    {
        if (! $this->ready()) {
            return;
        }

        // Only rows this migration inserted — an operator's own entry that
        // collided on the natural key was left alone by up() and must
        // survive down() too.
        DB::statement('DELETE FROM public_geo.commodity_aliases WHERE notes = ?', [self::NOTE]);
        DB::statement('DELETE FROM public_geo.status_aliases WHERE notes = ?', [self::NOTE]);
    }

    private const NOTE = 'Seeded 2026-08-20 from the reference cluster (Azure parity fix).';

    /**
     * Guard for non-Postgres and pre-2026-04-14 environments.
     *
     * The SQLite test bootstrap no-ops every raw CREATE TABLE, so neither
     * table exists there and an unconditional INSERT would fail the suite.
     */
    private function ready(): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return false;
        }

        foreach (['commodity_aliases', 'status_aliases'] as $table) {
            $present = DB::selectOne(
                'SELECT to_regclass(?) IS NOT NULL AS present',
                ["public_geo.{$table}"],
            )?->present ?? false;

            if (! $present) {
                return false;
            }
        }

        return true;
    }
};
