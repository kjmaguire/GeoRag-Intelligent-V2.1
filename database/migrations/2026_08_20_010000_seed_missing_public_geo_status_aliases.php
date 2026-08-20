<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Fill the public_geo.status_aliases gaps the live sync exposed.
 *
 * public_geo.pg_mine.status and pg_mineral_occurrence.status are
 * CHECK-constrained to a canonical vocabulary, and the sync resolves the
 * survey's raw label through status_aliases to get there. Anything unmapped
 * writes as 'unknown' — legal, but wrong, and it was silently applying to
 * 100% of the Saskatchewan mines: every one of the five values that
 * Mineral_Exploration/1 actually publishes was missing from the table, so
 * all 140 mines carried status='unknown' while the sync reported a clean run.
 *
 * The values below are not a guess or a sample. They are the complete
 * distinct sets returned by each layer's own
 * `/query?returnDistinctValues=true` on 2026-08-20:
 *
 *   Mineral_Exploration/1 (CA-SK mine)                 5 values, 5 missing
 *   Mineral_Exploration/5 (CA-SK SMDI occurrence)     19 values, 18 missing
 *   bcgwpub/137 (CA-BC MINFILE occurrence)             6 values, 0 missing
 *
 * Canonical vocabularies (from the 2026-04-14 canonical-tables migration):
 *   mine                producing, past-producer, developed-deposit,
 *                       prospect, closed, unknown
 *   mineral_occurrence  occurrence, showing, prospect, deposit,
 *                       past-producer, producer, unknown
 *
 * Two mapping judgements worth stating rather than burying:
 *
 *  1. "[Institutional Control Program]" is a Saskatchewan land-custody
 *     programme for decommissioned sites, not an exploration stage. The
 *     suffix is therefore ignored and each ICP value maps exactly as its
 *     unsuffixed twin does.
 *
 *  2. "Deposit: Production (Care and Maintenance)" maps to 'producer'
 *     because Saskatchewan classes it under Production and the canonical
 *     vocabulary has no suspended/idle member. A geologist may reasonably
 *     want it as 'past-producer'; that is a one-row UPDATE on this table,
 *     not a code change.
 *
 * Idempotent: ON CONFLICT DO NOTHING against the
 * (jurisdiction_code, canonical_type, source_value_lower) unique key, so an
 * operator who has already hand-added a row keeps their version.
 */
return new class extends Migration
{
    /**
     * Rows of [jurisdiction_code, canonical_type, source_value, canonical_status].
     *
     * @var list<array{0: string, 1: string, 2: string, 3: string}>
     */
    private const ALIASES = [
        // ── CA-SK mines (Mineral_Exploration/1) ──────────────────────────
        ['CA-SK', 'mine', 'Producing Mine', 'producing'],
        ['CA-SK', 'mine', 'Past Producing Mine with Reserves/Resources', 'past-producer'],
        ['CA-SK', 'mine', 'Past Producing Mine without Reserves/Resources', 'past-producer'],
        ['CA-SK', 'mine', 'Past-Producing Mine with Resources', 'past-producer'],
        ['CA-SK', 'mine', 'Past-Producing Mine without Resources', 'past-producer'],

        // ── CA-SK SMDI occurrences (Mineral_Exploration/5) ───────────────
        // Anomaly — a geochemical or geophysical response, not yet a showing.
        ['CA-SK', 'mineral_occurrence', 'Anomaly', 'occurrence'],
        ['CA-SK', 'mineral_occurrence', 'Anomaly: Bedrock/Felsenmeer Geochemical', 'occurrence'],
        ['CA-SK', 'mineral_occurrence', 'Anomaly: Geochemical', 'occurrence'],
        ['CA-SK', 'mineral_occurrence', 'Mineral Location', 'occurrence'],
        ['CA-SK', 'mineral_occurrence', 'Occurrence: Primary Exploration [Institutional Control Program]', 'occurrence'],

        ['CA-SK', 'mineral_occurrence', 'Prospect: Primary Exploration', 'prospect'],
        ['CA-SK', 'mineral_occurrence', 'Prospect: Primary Exploration [Institutional Control Program]', 'prospect'],

        // Deposit — a defined body, whatever study stage it has reached.
        ['CA-SK', 'mineral_occurrence', 'Deposit: Advanced Exploration', 'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Advanced Exploration (Bulk Sampling)', 'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Advanced Exploration (Bulk Sampling) [Institutional Control Program]', 'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Advanced Exploration [Institutional Control Program]', 'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Development', 'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Feasibility', 'deposit'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Prefeasibility', 'deposit'],

        // Production and post-production.
        ['CA-SK', 'mineral_occurrence', 'Deposit: Production', 'producer'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Production (Care and Maintenance)', 'producer'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Post-Production', 'past-producer'],
        ['CA-SK', 'mineral_occurrence', 'Deposit: Post-Production [Institutional Control Program]', 'past-producer'],
    ];

    public function up(): void
    {
        if (! $this->targetExists()) {
            return;
        }

        foreach (self::ALIASES as [$jurisdiction, $type, $sourceValue, $canonical]) {
            DB::statement(
                'INSERT INTO public_geo.status_aliases
                     (jurisdiction_code, canonical_type, source_value,
                      source_value_lower, canonical_status, notes,
                      created_at, updated_at)
                 VALUES (?, ?, ?, LOWER(?), ?, ?, NOW(), NOW())
                 ON CONFLICT (jurisdiction_code, canonical_type, source_value_lower)
                 DO NOTHING',
                [
                    $jurisdiction,
                    $type,
                    $sourceValue,
                    $sourceValue,
                    $canonical,
                    'Seeded 2026-08-20 from the layer\'s own returnDistinctValues response.',
                ],
            );
        }
    }

    public function down(): void
    {
        if (! $this->targetExists()) {
            return;
        }

        // Only remove rows this migration is responsible for — an operator's
        // hand-added mapping that collided on the unique key was left alone
        // by up() and must survive down() too, which the notes match ensures.
        foreach (self::ALIASES as [$jurisdiction, $type, $sourceValue, $canonical]) {
            DB::statement(
                'DELETE FROM public_geo.status_aliases
                  WHERE jurisdiction_code = ?
                    AND canonical_type = ?
                    AND source_value_lower = LOWER(?)
                    AND notes LIKE ?',
                [$jurisdiction, $type, $sourceValue, 'Seeded 2026-08-20 from%'],
            );
        }
    }

    /**
     * Guard for non-Postgres and pre-2026-04-14 environments.
     *
     * The SQLite test bootstrap no-ops every raw `CREATE TABLE`, so
     * public_geo.status_aliases does not exist there at all and an
     * unconditional INSERT would fail the whole Feature suite.
     */
    private function targetExists(): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return false;
        }

        return DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            ['public_geo.status_aliases'],
        )?->present ?? false;
    }
};
