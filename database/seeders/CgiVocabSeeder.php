<?php

declare(strict_types=1);

namespace Database\Seeders;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * Plan §1d — CGI controlled-vocabulary seeder.
 *
 * Seeds the four CGI vocabulary subsets Kyle picked (2026-05-29) into
 * silver.entity_aliases so the §2c entity resolver can map natural-
 * language terms in user queries to canonical CGI URIs:
 *
 *   1. CGI Simple Lithology      — rock types
 *   2. CGI Mineral Alteration    — hydrothermal alteration styles
 *   3. CGI Mineralisation Style  — deposit-type classifiers
 *   4. CGI Commodity             — element / mineral commodities
 *
 * Each JSON file under database/seeders/CgiVocab/ carries `_meta`
 * (vocab name, URI, entity_type, source label) + `entries` (list of
 * {canonical, uri, aliases[]}). The seeder walks each entry and inserts
 * one alias row per (canonical itself, …each variant). Idempotent via
 * the existing UNIQUE (workspace_id, entity_type, alias_normalised)
 * constraint — re-running the seeder UPDATEs existing rows in place
 * rather than throwing.
 *
 * Workspace handling: walks every row in silver.workspaces and seeds
 * the vocab per-workspace under the RLS GUC. CGI vocab is universal,
 * but RLS-per-workspace is the schema contract on silver.entity_aliases
 * — duplicating rows is the price of staying inside the tenancy model
 * (option 1 from §1d spec discussion).
 *
 * Source classification:
 *   • Each row sets source='cgi_vocab' (an existing CHECK-allowed value).
 *   • Each row's canonical_uri is the CGI resource URL so downstream
 *     callers can resolve the canonical entity beyond just the name.
 *
 * Run with:
 *     php artisan db:seed --class=CgiVocabSeeder
 *                         --database=pgsql_migrations
 *
 * Use the `pgsql_migrations` connection so the seeder runs as the
 * georag owner role (per project_pg_role_membership_gap memory) and
 * can bypass FORCE ROW LEVEL SECURITY when needed via SET LOCAL ROLE.
 */
class CgiVocabSeeder extends Seeder
{
    /**
     * Ordered list of vocab JSON files (relative to this directory).
     *
     * @var list<string>
     */
    private const VOCAB_FILES = [
        'CgiVocab/cgi_lithology.json',
        'CgiVocab/cgi_alteration.json',
        'CgiVocab/cgi_mineralization_style.json',
        'CgiVocab/cgi_commodity.json',
    ];

    public function run(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->command?->warn('CgiVocabSeeder: not pgsql driver — skipping');

            return;
        }

        $vocabs = $this->loadVocabs();
        $this->command?->info(sprintf(
            'CgiVocabSeeder: loaded %d vocab files with %d total canonical entries',
            count($vocabs),
            array_sum(array_map(fn ($v) => count($v['entries']), $vocabs)),
        ));

        $workspaceIds = $this->workspaceIds();
        if (empty($workspaceIds)) {
            $this->command?->warn('CgiVocabSeeder: no rows in silver.workspaces — nothing to seed');

            return;
        }
        $this->command?->info(sprintf(
            'CgiVocabSeeder: seeding into %d workspace(s)',
            count($workspaceIds),
        ));

        $insertedTotal = 0;
        $updatedTotal = 0;
        foreach ($workspaceIds as $workspaceId) {
            [$inserted, $updated] = $this->seedWorkspace($workspaceId, $vocabs);
            $insertedTotal += $inserted;
            $updatedTotal += $updated;
        }

        $this->command?->info(sprintf(
            'CgiVocabSeeder: complete — %d aliases inserted, %d updated across %d workspace(s)',
            $insertedTotal,
            $updatedTotal,
            count($workspaceIds),
        ));
    }

    /**
     * Load + decode the vocab JSON files. Returns one array per vocab
     * with shape `[_meta => [...], entries => [{canonical, uri, aliases}]]`.
     *
     * @return list<array{_meta: array<string, mixed>, entries: list<array{canonical: string, uri: string|null, aliases: list<string>}>}>
     */
    private function loadVocabs(): array
    {
        $vocabs = [];
        foreach (self::VOCAB_FILES as $relPath) {
            $path = __DIR__.DIRECTORY_SEPARATOR.$relPath;
            if (! is_file($path)) {
                throw new \RuntimeException("CgiVocabSeeder: vocab file missing: {$relPath}");
            }
            $raw = file_get_contents($path);
            if ($raw === false) {
                throw new \RuntimeException("CgiVocabSeeder: failed reading {$relPath}");
            }
            $decoded = json_decode($raw, true);
            if (! is_array($decoded) || ! isset($decoded['_meta'], $decoded['entries'])) {
                throw new \RuntimeException("CgiVocabSeeder: malformed JSON in {$relPath}");
            }
            $vocabs[] = $decoded;
        }

        return $vocabs;
    }

    /**
     * Workspace IDs to seed. Returns string UUIDs.
     *
     * @return list<string>
     */
    private function workspaceIds(): array
    {
        return array_map(
            static fn ($row) => (string) $row->workspace_id,
            DB::select('SELECT workspace_id::text AS workspace_id FROM silver.workspaces'),
        );
    }

    /**
     * Seed one workspace with all four vocabs.
     *
     * Returns [inserted_count, updated_count].
     *
     * The seeder sets georag.workspace_id GUC inside a single transaction
     * so RLS lets the INSERTs through. Uses ON CONFLICT DO UPDATE to
     * keep the seeder idempotent — rerunning bumps updated_at on
     * existing rows without duplicating.
     *
     * @param list<array{_meta: array<string, mixed>, entries: list<array{canonical: string, uri: string|null, aliases: list<string>}>}> $vocabs
     *
     * @return array{0: int, 1: int}
     */
    private function seedWorkspace(string $workspaceId, array $vocabs): array
    {
        $inserted = 0;
        $updated = 0;

        DB::transaction(function () use ($workspaceId, $vocabs, &$inserted, &$updated): void {
            DB::statement("SELECT set_config('georag.workspace_id', ?, true)", [$workspaceId]);

            foreach ($vocabs as $vocab) {
                $entityType = (string) $vocab['_meta']['entity_type'];
                $source = (string) $vocab['_meta']['source'];

                foreach ($vocab['entries'] as $entry) {
                    $canonical = (string) $entry['canonical'];
                    $uri = $entry['uri'] !== null ? (string) $entry['uri'] : null;

                    // Every entry seeds at minimum the canonical name as its own
                    // alias — so an exact-match lookup on the canonical string
                    // resolves without needing the "alias is the canonical"
                    // special case in the resolver.
                    $aliasSet = array_unique(array_merge([$canonical], $entry['aliases']));

                    foreach ($aliasSet as $alias) {
                        $alias = (string) $alias;
                        $aliasNormalised = $this->normaliseAlias($alias);
                        if ($aliasNormalised === '') {
                            continue;
                        }

                        $result = DB::selectOne(
                            <<<'SQL'
                            INSERT INTO silver.entity_aliases (
                                workspace_id, entity_type,
                                canonical_name, canonical_uri,
                                alias, alias_normalised,
                                source, confidence,
                                created_at, updated_at
                            ) VALUES (
                                ?::uuid, ?,
                                ?, ?,
                                ?, ?,
                                ?, 1.000,
                                NOW(), NOW()
                            )
                            ON CONFLICT (workspace_id, entity_type, alias_normalised)
                            DO UPDATE SET
                                canonical_name = EXCLUDED.canonical_name,
                                canonical_uri  = EXCLUDED.canonical_uri,
                                alias          = EXCLUDED.alias,
                                source         = EXCLUDED.source,
                                updated_at     = NOW()
                            RETURNING (xmax = 0) AS is_insert
                            SQL,
                            [
                                $workspaceId, $entityType,
                                $canonical, $uri,
                                $alias, $aliasNormalised,
                                $source,
                            ],
                        );

                        if ($result !== null && (bool) $result->is_insert) {
                            $inserted++;
                        } else {
                            $updated++;
                        }
                    }
                }
            }
        });

        return [$inserted, $updated];
    }

    /**
     * Normalise an alias for the unique-key lookup: lowercase, collapse
     * whitespace, strip surrounding spaces. The §2c resolver applies
     * the same normalisation to incoming query terms so an entry like
     * "Mississippi Valley-type deposit" matches a query asking about
     * "  Mississippi  Valley-type  ".
     *
     * Keeps non-ASCII characters intact (e.g. "U₃O₈") because the
     * resolver compares strings byte-for-byte after this transform —
     * stripping diacritics here would prevent unicode aliases from
     * matching themselves.
     */
    private function normaliseAlias(string $alias): string
    {
        $lower = mb_strtolower($alias, 'UTF-8');
        $collapsed = preg_replace('/\s+/u', ' ', $lower) ?? $lower;

        return trim($collapsed);
    }
}
