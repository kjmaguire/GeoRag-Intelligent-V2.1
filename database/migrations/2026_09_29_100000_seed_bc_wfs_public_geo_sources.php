<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Register the three BC public-geo feeds that come from DataBC's WFS, and the
 * status aliases the BC mine feed needs.
 *
 * Every public_geo.pg_* table foreign-keys public_geo.sources(source_id), so a
 * feed the sync knows about (src/fastapi/app/services/public_geo/registry.py)
 * but no migration declares fails every row write on the FK. The addressing
 * itself lives in the registry, not here — these rows exist to satisfy the
 * foreign key and to describe the feed; see the registry docstring for why.
 *
 *   CA-BC-MINFILE-MINES     mine                 WHSE_MINERAL_TENURE.MINFIL_MINERAL_OCCURRENCE
 *                                                (filtered to Producer / Past Producer)
 *   CA-BC-MTA-TENURE        mineral_disposition  WHSE_MINERAL_TENURE.MTA_ACQUIRED_TENURE_SVW
 *   CA-BC-GEOLOGY-BEDROCK   bedrock_geology      WHSE_MINERAL_TENURE.GEOL_BEDROCK_UNIT_POLY_SVW
 *
 * All three are [UNVERIFIED] against the live service — the environment that
 * wrote them could not reach openmaps.gov.bc.ca. Run
 * ops/validation/public_geo_probe.py from inside AWS to confirm.
 *
 * service_url is the WFS endpoint and layer_index is NULL: a WFS feed is
 * addressed by its BCGW object name (recorded in notes), not a layer number.
 *
 * Status aliases: pg_mine.status is CHECK-constrained to the canonical
 * vocabulary (producing, past-producer, …) and the sync resolves MINFILE's
 * label through public_geo.status_aliases scoped by (jurisdiction,
 * canonical_type). CA-BC had rows only for canonical_type =
 * 'mineral_occurrence'; without these every BC mine would write 'unknown'.
 * "Producer" / "Past Producer" are MINFILE's own values (returnDistinctValues
 * on bcgwpub/137, 2026-08-20 — see 2026_08_20_010000); "Producing" is carried
 * because the occurrence aliases already map it.
 *
 * Idempotent (upsert on sources, DO NOTHING on aliases); a no-op off Postgres
 * or before the public_geo chain has run, same guard as its siblings.
 */
return new class extends Migration
{
    private const LICENCE = 'Open Government Licence – British Columbia (v2.0)';

    private const LICENCE_URL = 'https://www2.gov.bc.ca/gov/content/data/open-data/open-government-licence-bc';

    private const WFS = 'https://openmaps.gov.bc.ca/geo/ows';

    private const ALIAS_NOTE = 'Seeded 2026-09-29 for CA-BC-MINFILE-MINES (MINFILE status labels).';

    /**
     * @var list<array<string, mixed>>
     */
    private const SOURCES = [
        [
            'source_id' => 'CA-BC-MINFILE-MINES',
            'jurisdiction_code' => 'CA-BC',
            'name' => 'BC MINFILE — Producers and Past Producers (mines)',
            'canonical_type' => 'mine',
            'service_url' => self::WFS,
            'layer_index' => null,
            'source_crs' => 3005,
            'license_summary' => self::LICENCE,
            'license_url' => self::LICENCE_URL,
            'refresh_cadence' => 'weekly',
            'notes' => 'DataBC WFS typeName pub:WHSE_MINERAL_TENURE.MINFIL_MINERAL_OCCURRENCE, '
                ."CQL_FILTER STATUS_DESCRIPTION IN ('Producer','Past Producer'), sortBy MINFILE_NUMBER. "
                .'Catalogue slug minfile-mineral-occurrence-database. [UNVERIFIED] until public_geo_probe passes.',
        ],
        [
            'source_id' => 'CA-BC-MTA-TENURE',
            'jurisdiction_code' => 'CA-BC',
            'name' => 'BC Mineral, Placer and Coal Tenure (Mineral Titles)',
            'canonical_type' => 'mineral_disposition',
            'service_url' => self::WFS,
            'layer_index' => null,
            'source_crs' => 3005,
            'license_summary' => self::LICENCE,
            'license_url' => self::LICENCE_URL,
            'refresh_cadence' => 'weekly',
            'notes' => 'DataBC WFS typeName pub:WHSE_MINERAL_TENURE.MTA_ACQUIRED_TENURE_SVW, sortBy TENURE_NUMBER_ID. '
                .'Catalogue slug mta-mineral-placer-and-coal-tenure-spatial-view. Placer titles are not written '
                .'(no placer member in the disposition_type CHECK). [UNVERIFIED] until public_geo_probe passes.',
        ],
        [
            'source_id' => 'CA-BC-GEOLOGY-BEDROCK',
            'jurisdiction_code' => 'CA-BC',
            'name' => 'BC Digital Geology — Bedrock Units',
            'canonical_type' => 'bedrock_geology',
            'service_url' => self::WFS,
            'layer_index' => null,
            'source_crs' => 3005,
            'license_summary' => self::LICENCE,
            'license_url' => self::LICENCE_URL,
            'refresh_cadence' => 'weekly',
            'notes' => 'DataBC WFS typeName pub:WHSE_MINERAL_TENURE.GEOL_BEDROCK_UNIT_POLY_SVW, sortBy OBJECTID. '
                .'Catalogue slug bedrock-geology. [UNVERIFIED] until public_geo_probe passes.',
        ],
    ];

    /**
     * Rows of [jurisdiction_code, canonical_type, source_value, canonical_status].
     *
     * @var list<array{0: string, 1: string, 2: string, 3: string}>
     */
    private const ALIASES = [
        ['CA-BC', 'mine', 'Producer', 'producing'],
        ['CA-BC', 'mine', 'Producing', 'producing'],
        ['CA-BC', 'mine', 'Past Producer', 'past-producer'],
    ];

    public function up(): void
    {
        if (! $this->tableExists('sources')) {
            return;
        }

        foreach (self::SOURCES as $s) {
            $columns = array_keys($s);
            $placeholders = implode(', ', array_fill(0, count($columns), '?'));
            $columnList = implode(', ', $columns);
            $updates = implode(', ', array_map(
                static fn (string $c): string => "{$c} = EXCLUDED.{$c}",
                $columns,
            ));

            DB::insert(
                "INSERT INTO public_geo.sources ({$columnList}, created_at, updated_at)
                 VALUES ({$placeholders}, now(), now())
                 ON CONFLICT (source_id) DO UPDATE SET {$updates}, updated_at = now()",
                array_values($s),
            );
        }

        if (! $this->tableExists('status_aliases')) {
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
                [$jurisdiction, $type, $sourceValue, $sourceValue, $canonical, self::ALIAS_NOTE],
            );
        }
    }

    /**
     * Removes only the alias rows this migration wrote (matched on notes).
     *
     * The source rows are left in place: once a sync has written features
     * against them, the pg_* foreign keys (ON DELETE RESTRICT) make deleting
     * them fail, and an empty registry row is harmless.
     */
    public function down(): void
    {
        if (! $this->tableExists('status_aliases')) {
            return;
        }

        foreach (self::ALIASES as [$jurisdiction, $type, $sourceValue]) {
            DB::statement(
                'DELETE FROM public_geo.status_aliases
                  WHERE jurisdiction_code = ?
                    AND canonical_type = ?
                    AND source_value_lower = LOWER(?)
                    AND notes = ?',
                [$jurisdiction, $type, $sourceValue, self::ALIAS_NOTE],
            );
        }
    }

    private function tableExists(string $table): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return false;
        }

        return DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            ["public_geo.{$table}"],
        )?->present ?? false;
    }
};
