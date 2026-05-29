<?php

declare(strict_types=1);

namespace Tests\Feature\Seeders;

use Database\Seeders\CgiVocabSeeder;
use Illuminate\Support\Facades\Artisan;
use Illuminate\Support\Facades\DB;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Plan §1d — CgiVocabSeeder behaviour.
 *
 * Verifies the four CGI vocab JSON files seed correctly into
 * silver.entity_aliases under the workspace RLS GUC and that the
 * seeder is idempotent (re-runs update rather than throw).
 *
 * Gated on Postgres because silver.entity_aliases lives in the silver
 * schema with RLS enabled; sqlite tests skip cleanly.
 */
class CgiVocabSeederTest extends TestCase
{
    use RequiresPostgres;

    /**
     * Test workspace UUID — distinct from the dev fixture
     * (a0000000-...) so the seeder rows are isolated and we can
     * tearDown deterministically.
     */
    private const TEST_WORKSPACE_ID = '00000000-0000-0000-0000-00000000cccc';

    protected function setUp(): void
    {
        // Skip BEFORE parent::setUp() so we never touch sqlite with
        // pg-specific DDL. Mirrors RequiresPostgres::setUp() — we have
        // to do it explicitly here because overriding setUp() shadows
        // the trait method.
        $conn = $_SERVER['DB_CONNECTION']
            ?? $_ENV['DB_CONNECTION']
            ?? getenv('DB_CONNECTION')
            ?: 'sqlite';
        if ($conn !== 'pgsql') {
            $this->markTestSkipped(
                'CgiVocabSeederTest requires the postgres test connection.',
            );

            return;
        }

        parent::setUp();
        $this->ensureTestWorkspace();
        $this->scrubTestWorkspaceAliases();
    }

    protected function tearDown(): void
    {
        // Mirror the setUp skip so tearDown doesn't try to DELETE on
        // sqlite (PHPUnit calls tearDown even after markTestSkipped).
        $conn = $_SERVER['DB_CONNECTION']
            ?? $_ENV['DB_CONNECTION']
            ?? getenv('DB_CONNECTION')
            ?: 'sqlite';
        if ($conn === 'pgsql') {
            $this->scrubTestWorkspaceAliases();
        }
        parent::tearDown();
    }

    // ---------------------------------------------------------------------
    // JSON file integrity
    // ---------------------------------------------------------------------

    public function test_all_four_vocab_files_parse_with_expected_shape(): void
    {
        $files = [
            'cgi_lithology.json',
            'cgi_alteration.json',
            'cgi_mineralization_style.json',
            'cgi_commodity.json',
        ];

        foreach ($files as $file) {
            $path = database_path("seeders/CgiVocab/{$file}");
            $this->assertFileExists($path, "missing vocab file: {$file}");

            $decoded = json_decode((string) file_get_contents($path), true);
            $this->assertIsArray($decoded, "vocab file did not decode: {$file}");
            $this->assertArrayHasKey('_meta', $decoded, "missing _meta in {$file}");
            $this->assertArrayHasKey('entries', $decoded, "missing entries in {$file}");

            // Meta carries the entity_type used during INSERT.
            $this->assertArrayHasKey('entity_type', $decoded['_meta'], "missing entity_type in {$file} _meta");
            $this->assertContains(
                $decoded['_meta']['entity_type'],
                ['property', 'project', 'company', 'commodity', 'hole_id',
                    'formation', 'document_type', 'technical_term', 'mineral', 'method'],
                "{$file} _meta.entity_type must be one of the CHECK-allowed values",
            );

            $this->assertNotEmpty($decoded['entries'], "{$file} has zero entries");
            foreach ($decoded['entries'] as $i => $entry) {
                $this->assertIsString($entry['canonical'] ?? null, "{$file} entry #{$i} missing canonical string");
                $this->assertArrayHasKey('aliases', $entry, "{$file} entry #{$i} missing aliases array");
                $this->assertIsArray($entry['aliases'], "{$file} entry #{$i} aliases not an array");
            }
        }
    }

    // ---------------------------------------------------------------------
    // Seeding produces rows
    // ---------------------------------------------------------------------

    public function test_seeder_populates_entity_aliases_for_test_workspace(): void
    {
        $this->runSeeder();

        // Should have rows for all four entity_type / source combinations
        // we expect to seed (lithology + alteration + mineralization_style
        // → 'technical_term'; commodity → 'commodity').
        $technicalTermCount = $this->aliasCountForType('technical_term');
        $commodityCount = $this->aliasCountForType('commodity');

        $this->assertGreaterThan(50, $technicalTermCount,
            'expected at least 50 technical_term aliases (lithology + alteration + mineralization)');
        $this->assertGreaterThan(15, $commodityCount,
            'expected at least 15 commodity aliases');
    }

    public function test_seeder_inserts_canonical_name_as_its_own_alias(): void
    {
        $this->runSeeder();

        // Every entry seeds the canonical as alias[0] so an exact-match
        // lookup on "gold" resolves to canonical "gold" without
        // special-casing in the resolver.
        $hit = $this->aliasRow('commodity', 'gold');
        $this->assertNotNull($hit, 'canonical "gold" should be seeded as its own alias');
        $this->assertEquals('gold', $hit->canonical_name);
        $this->assertStringContainsString('commodity-code', (string) $hit->canonical_uri,
            'commodity URI should point to CGI commodity-code namespace');
    }

    public function test_seeder_resolves_element_symbols_to_canonical_names(): void
    {
        $this->runSeeder();

        // The whole point of the alias table — element symbols resolve
        // to the full canonical name.
        $hit = $this->aliasRow('commodity', 'au');
        $this->assertNotNull($hit, 'element symbol "Au" should resolve to canonical');
        $this->assertEquals('gold', $hit->canonical_name);

        $cu = $this->aliasRow('commodity', 'cu');
        $this->assertNotNull($cu);
        $this->assertEquals('copper', $cu->canonical_name);

        // Compound assay unit
        $u3o8 = $this->aliasRow('commodity', 'u3o8');
        $this->assertNotNull($u3o8, 'U3O8 should resolve to canonical uranium');
        $this->assertEquals('uranium', $u3o8->canonical_name);
    }

    public function test_seeder_resolves_deposit_style_acronyms(): void
    {
        $this->runSeeder();

        // VMS / IOCG / SEDEX — the acronyms geologists actually type.
        $vms = $this->aliasRow('technical_term', 'vms');
        $this->assertNotNull($vms, '"VMS" should resolve to canonical');
        $this->assertEquals('volcanogenic massive sulfide deposit', $vms->canonical_name);

        $iocg = $this->aliasRow('technical_term', 'iocg');
        $this->assertNotNull($iocg);
        $this->assertEquals('iron oxide copper-gold deposit', $iocg->canonical_name);
    }

    public function test_seeder_resolves_alteration_spelling_variants(): void
    {
        $this->runSeeder();

        // British vs American spellings of -isation/-ization both map
        // to the canonical (-isation, per CGI convention). The
        // sericitisation entry is its own canonical (CGI treats it as
        // a distinct concept from phyllic alteration).
        $american = $this->aliasRow('technical_term', 'sericitization');
        $this->assertNotNull($american);
        $this->assertEquals('sericitisation', $american->canonical_name,
            'sericitization (American spelling) should map to canonical sericitisation');

        // Phyllic alteration's canonical is "phyllic alteration" itself.
        // It owns aliases like "QSP", "sericite", "quartz-sericite-pyrite"
        // — but NOT "sericitisation" (that's a distinct CGI entry).
        $phyllic = $this->aliasRow('technical_term', 'phyllic alteration');
        $this->assertNotNull($phyllic);
        $this->assertEquals('phyllic alteration', $phyllic->canonical_name);

        $qsp = $this->aliasRow('technical_term', 'qsp');
        $this->assertNotNull($qsp);
        $this->assertEquals('phyllic alteration', $qsp->canonical_name);
    }

    // ---------------------------------------------------------------------
    // Idempotency
    // ---------------------------------------------------------------------

    public function test_seeder_is_idempotent_on_rerun(): void
    {
        $this->runSeeder();
        $firstRunCount = $this->totalAliasCount();

        // Second run — must not throw, must not duplicate.
        $this->runSeeder();
        $secondRunCount = $this->totalAliasCount();

        $this->assertEquals($firstRunCount, $secondRunCount,
            'seeder must be idempotent — re-running should not change row count');
        $this->assertGreaterThan(0, $firstRunCount);
    }

    public function test_seeder_updates_existing_rows_rather_than_skipping(): void
    {
        // First run creates rows.
        $this->runSeeder();
        $firstUpdate = $this->lastUpdatedFor('commodity', 'gold');

        // Wait a hair so updated_at moves measurably (NOW() advances).
        usleep(50_000);  // 50 ms

        // Second run hits ON CONFLICT DO UPDATE → updated_at bumps.
        $this->runSeeder();
        $secondUpdate = $this->lastUpdatedFor('commodity', 'gold');

        $this->assertGreaterThan($firstUpdate, $secondUpdate,
            'ON CONFLICT DO UPDATE should bump updated_at on re-run');
    }

    // ---------------------------------------------------------------------
    // Normalisation behaviour
    // ---------------------------------------------------------------------

    public function test_alias_normalised_is_lowercase_collapsed_whitespace(): void
    {
        $this->runSeeder();

        // "Mississippi Valley-type deposit" normalises to lowercase
        // exactly because there's no extra whitespace in the source.
        $hit = $this->aliasRow('technical_term', 'mississippi valley-type deposit');
        $this->assertNotNull($hit);
        $this->assertEquals('Mississippi Valley-type deposit', $hit->canonical_name);

        // Alias "MVT" (uppercase) seeded as alias_normalised='mvt'
        $mvt = $this->aliasRow('technical_term', 'mvt');
        $this->assertNotNull($mvt);
        $this->assertEquals('Mississippi Valley-type deposit', $mvt->canonical_name);
    }

    // ---------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------

    private function runSeeder(): void
    {
        $this->setWorkspaceGuc();
        Artisan::call('db:seed', [
            '--class' => CgiVocabSeeder::class,
            '--force' => true,
        ]);
    }

    private function setWorkspaceGuc(): void
    {
        DB::statement("SELECT set_config('georag.workspace_id', ?, true)",
            [self::TEST_WORKSPACE_ID]);
    }

    private function ensureTestWorkspace(): void
    {
        // silver.workspaces has a NOT NULL `slug` column added after the
        // initial schema — include it for the test fixture insert.
        DB::statement(
            "INSERT INTO silver.workspaces (workspace_id, name, slug)
             VALUES (?::uuid, 'cgi-vocab-seeder-test', 'cgi-vocab-seeder-test')
             ON CONFLICT DO NOTHING",
            [self::TEST_WORKSPACE_ID],
        );
    }

    private function scrubTestWorkspaceAliases(): void
    {
        $this->setWorkspaceGuc();
        DB::statement(
            'DELETE FROM silver.entity_aliases WHERE workspace_id = ?::uuid',
            [self::TEST_WORKSPACE_ID],
        );
    }

    private function aliasCountForType(string $entityType): int
    {
        $this->setWorkspaceGuc();
        $row = DB::selectOne(
            'SELECT count(*)::int AS n
             FROM silver.entity_aliases
             WHERE workspace_id = ?::uuid AND entity_type = ?',
            [self::TEST_WORKSPACE_ID, $entityType],
        );

        return (int) ($row->n ?? 0);
    }

    private function totalAliasCount(): int
    {
        $this->setWorkspaceGuc();
        $row = DB::selectOne(
            'SELECT count(*)::int AS n FROM silver.entity_aliases WHERE workspace_id = ?::uuid',
            [self::TEST_WORKSPACE_ID],
        );

        return (int) ($row->n ?? 0);
    }

    private function aliasRow(string $entityType, string $aliasNormalised): ?object
    {
        $this->setWorkspaceGuc();

        return DB::selectOne(
            'SELECT canonical_name, canonical_uri, alias, alias_normalised, source
             FROM silver.entity_aliases
             WHERE workspace_id = ?::uuid
               AND entity_type = ?
               AND alias_normalised = ?
             LIMIT 1',
            [self::TEST_WORKSPACE_ID, $entityType, $aliasNormalised],
        );
    }

    private function lastUpdatedFor(string $entityType, string $aliasNormalised): string
    {
        $this->setWorkspaceGuc();
        $row = DB::selectOne(
            "SELECT to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') AS ts
             FROM silver.entity_aliases
             WHERE workspace_id = ?::uuid
               AND entity_type = ?
               AND alias_normalised = ?
             LIMIT 1",
            [self::TEST_WORKSPACE_ID, $entityType, $aliasNormalised],
        );

        return (string) ($row->ts ?? '');
    }
}
