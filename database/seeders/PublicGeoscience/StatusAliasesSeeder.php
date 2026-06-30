<?php

declare(strict_types=1);

namespace Database\Seeders\PublicGeoscience;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * Seed the `public_geo.status_aliases` crosswalk.
 *
 * Scoped per (jurisdiction_code, canonical_type) because the same raw
 * string (e.g. "Producer") may carry different canonical meanings in
 * different feature layers.
 *
 * Initial seed focuses on Saskatchewan — CA-SK Mine Locations (layer 1)
 * and CA-SK SMDI (layer 2). Drillhole disposition values are NOT mapped
 * here: pg_drillhole_collar has no status enum, only `core_availability`
 * (seeded inline in Silver ingestion, Phase 2.2).
 *
 * Unmapped raw values fall through to 'unknown' at Silver tier and are
 * logged; add a row here rather than hardcoding in Python (plan §04c).
 */
class StatusAliasesSeeder extends Seeder
{
    /**
     * Each row: [jurisdiction_code, canonical_type, source_value, canonical_status]
     *
     * canonical_status enums (plan §04a):
     *   mine                 → producing | past-producer | developed-deposit | prospect | closed | unknown
     *   mineral_occurrence   → occurrence | showing | prospect | deposit | past-producer | producer | unknown
     */
    private const ROWS = [
        // ── CA-SK Mine Locations (layer 1) ───────────────────────────
        ['CA-SK', 'mine', 'Producer',                          'producing'],
        ['CA-SK', 'mine', 'Producing',                         'producing'],
        ['CA-SK', 'mine', 'Active',                            'producing'],
        ['CA-SK', 'mine', 'Past Producer',                     'past-producer'],
        ['CA-SK', 'mine', 'Past-Producer',                     'past-producer'],
        ['CA-SK', 'mine', 'Former Producer',                   'past-producer'],
        ['CA-SK', 'mine', 'Developed Deposit',                 'developed-deposit'],
        ['CA-SK', 'mine', 'Development',                       'developed-deposit'],
        ['CA-SK', 'mine', 'Prospect',                          'prospect'],
        ['CA-SK', 'mine', 'Advanced Prospect',                 'prospect'],
        ['CA-SK', 'mine', 'Closed',                            'closed'],
        ['CA-SK', 'mine', 'Abandoned',                         'closed'],
        ['CA-SK', 'mine', 'Reclaimed',                         'closed'],
        ['CA-SK', 'mine', 'Unknown',                           'unknown'],
        ['CA-SK', 'mine', 'Not Specified',                     'unknown'],

        // ── CA-SK SMDI (layer 2) ─────────────────────────────────────
        // SMDI status vocabulary per the published domain.
        ['CA-SK', 'mineral_occurrence', 'Occurrence',                      'occurrence'],
        ['CA-SK', 'mineral_occurrence', 'Occurrence: Primary Exploration', 'occurrence'],
        ['CA-SK', 'mineral_occurrence', 'Showing',                         'showing'],
        ['CA-SK', 'mineral_occurrence', 'Prospect',                        'prospect'],
        ['CA-SK', 'mineral_occurrence', 'Advanced Prospect',               'prospect'],
        ['CA-SK', 'mineral_occurrence', 'Deposit',                         'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Developed Deposit',               'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Past Producer',                   'past-producer'],
        ['CA-SK', 'mineral_occurrence', 'Past-Producer',                   'past-producer'],
        ['CA-SK', 'mineral_occurrence', 'Former Producer',                 'past-producer'],
        ['CA-SK', 'mineral_occurrence', 'Producer',                        'producer'],
        ['CA-SK', 'mineral_occurrence', 'Producing',                       'producer'],
        ['CA-SK', 'mineral_occurrence', 'Active Producer',                 'producer'],
        ['CA-SK', 'mineral_occurrence', 'Unknown',                         'unknown'],
        ['CA-SK', 'mineral_occurrence', 'Not Specified',                   'unknown'],

        // ── CA-BC MINFILE (Phase 4 — second jurisdiction) ────────────
        // BC MINFILE status domain. Values here are per the published
        // MINFILE 2.0 STATUS_DESC enumeration; unmatched upstream values
        // fall through to 'unknown' at Silver and get logged so Kyle can
        // extend this list without code changes.
        ['CA-BC', 'mineral_occurrence', 'Showing',                         'showing'],
        ['CA-BC', 'mineral_occurrence', 'Prospect',                        'prospect'],
        ['CA-BC', 'mineral_occurrence', 'Developed Prospect',              'prospect'],
        ['CA-BC', 'mineral_occurrence', 'Anomaly',                         'occurrence'],
        ['CA-BC', 'mineral_occurrence', 'Occurrence',                      'occurrence'],
        ['CA-BC', 'mineral_occurrence', 'Past Producer',                   'past-producer'],
        ['CA-BC', 'mineral_occurrence', 'Producer',                        'producer'],
        ['CA-BC', 'mineral_occurrence', 'Producing',                       'producer'],
        ['CA-BC', 'mineral_occurrence', 'Unknown',                         'unknown'],
    ];

    public function run(): void
    {
        $now = now();

        foreach (self::ROWS as [$juris, $type, $sourceValue, $canonical]) {
            DB::table('public_geo.status_aliases')->updateOrInsert(
                [
                    'jurisdiction_code' => $juris,
                    'canonical_type' => $type,
                    'source_value_lower' => mb_strtolower($sourceValue),
                ],
                [
                    'source_value' => $sourceValue,
                    'canonical_status' => $canonical,
                    'updated_at' => $now,
                    'created_at' => $now,
                ],
            );
        }

        $this->command?->info('Seeded '.count(self::ROWS).' status aliases (Saskatchewan).');
    }
}
