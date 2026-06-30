<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Models\User;
use Carbon\Carbon;
use Illuminate\Database\Query\Expression;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Feature tests for CitationController — Public Geoscience resolver paths.
 *
 * Route: GET /api/v1/citations/resolve?source_chunk_id=...  (auth:sanctum)
 *
 * All PGEO resolvers query public_geoscience.* or silver.* tables that SQLite
 * cannot emulate. DB::table() chains are mocked via DB::shouldReceive() so
 * every assertion stays at the HTTP-contract layer.
 */
class CitationControllerPgeoTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    /** Well-formed UUID used as pg_id across all PGEO resolver tests. */
    private const PG_ID = 'a1b2c3d4-0000-0000-0000-000000000001';

    protected function setUp(): void
    {
        parent::setUp();
        $this->user = User::factory()->create();
    }

    // ── 400 — missing source_chunk_id ────────────────────────────────────────

    public function test_missing_source_chunk_id_returns_400(): void
    {
        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve')
            ->assertStatus(400)
            ->assertJsonPath('message', 'source_chunk_id is required');
    }

    // ── Unknown prefix ───────────────────────────────────────────────────────

    public function test_unknown_prefix_returns_source_type_unknown(): void
    {
        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id=totally_unknown:foo')
            ->assertOk()
            ->assertJsonPath('source_type', 'unknown');
    }

    // ── pg_mine resolver ─────────────────────────────────────────────────────

    public function test_pg_mine_resolver_returns_pgeo_envelope(): void
    {
        $this->mockPgeoResolverCall(
            entityTable: 'public_geoscience.pg_mine',
            entityRow: $this->fakeMineRow(),
            canonicalType: 'mine',
        );

        $chunkId = 'pg_mine:CA-SK-MINE-LOC:feature=12345:pg_id='.self::PG_ID;

        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk()
            ->assertJsonPath('source_type', 'public_geoscience')
            ->assertJsonPath('corpus', 'public_geoscience')
            ->assertJsonPath('canonical_type', 'mine')
            ->assertJsonStructure([
                'source_type', 'corpus', 'canonical_type', 'source_chunk_id',
                'jurisdiction' => ['code', 'name', 'authority'],
                'source' => ['source_id', 'name', 'service_url'],
                'license' => ['summary', 'url'],
                'refresh' => ['last_refreshed_at', 'staleness_seconds'],
                'references_summary' => ['count', 'documents'],
                'title', 'text', 'entity', 'metadata',
            ]);
    }

    // ── pg_mineral_occurrence resolver ────────────────────────────────────────

    public function test_pg_mineral_occurrence_resolver_returns_pgeo_envelope(): void
    {
        $entityRow = [
            'id' => self::PG_ID,
            'jurisdiction_code' => 'CA-SK',
            'source_id' => 'CA-SK-SMDI',
            'source_feature_id' => '7788',
            'external_id' => 'SK-1234',
            'name' => 'North Lake Showing',
            'historic_names' => '{}',
            'status' => 'showing',
            'primary_commodities' => '{uranium}',
            'associated_commodities' => '{}',
            'commodity_grouping' => 'uranium',
            'discovery_type' => 'outcrop',
            'production_flag' => false,
            'reserves_resources' => null,
            'source_url' => 'https://example.com/smdi/7788',
            'last_seen_at' => '2026-01-01 00:00:00',
        ];

        $this->mockPgeoResolverCall(
            entityTable: 'public_geoscience.pg_mineral_occurrence',
            entityRow: $entityRow,
            canonicalType: 'mineral_occurrence',
        );

        $chunkId = 'pg_mineral_occurrence:CA-SK-SMDI:feature=7788:pg_id='.self::PG_ID;

        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk()
            ->assertJsonPath('source_type', 'public_geoscience')
            ->assertJsonPath('canonical_type', 'mineral_occurrence')
            ->assertJsonStructure([
                'source_type', 'corpus', 'canonical_type', 'source_chunk_id',
                'jurisdiction', 'source', 'license', 'refresh',
                'references_summary', 'title', 'text', 'entity', 'metadata',
            ]);
    }

    // ── pg_drillhole_collar resolver ──────────────────────────────────────────

    public function test_pg_drillhole_collar_resolver_returns_pgeo_envelope(): void
    {
        $entityRow = [
            'id' => self::PG_ID,
            'jurisdiction_code' => 'CA-SK',
            'source_id' => 'CA-SK-DRILLHOLE',
            'source_feature_id' => '9001',
            'drillhole_id' => 'GOS-9001',
            'drillhole_name' => 'PLS-20-001',
            'company' => 'TestDrill Inc.',
            'project_name' => 'Patterson Lake South',
            'date_drilled' => '2020-06-15',
            'drill_type' => 'core',
            'commodity_of_interest' => '{uranium}',
            'total_length_m' => '350.5',
            'collar_elevation_m' => '420.0',
            'stratigraphic_depths' => null,
            'core_availability' => 'available',
            'core_storage' => 'SMRF',
            'disposition' => null,
            'source_url' => null,
            'last_seen_at' => null,
        ];

        $this->mockPgeoResolverCall(
            entityTable: 'public_geoscience.pg_drillhole_collar',
            entityRow: $entityRow,
            canonicalType: 'drillhole_collar',
        );

        $chunkId = 'pg_drillhole_collar:CA-SK-DRILLHOLE:feature=9001:pg_id='.self::PG_ID;

        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk()
            ->assertJsonPath('source_type', 'public_geoscience')
            ->assertJsonPath('canonical_type', 'drillhole_collar')
            ->assertJsonStructure([
                'source_type', 'corpus', 'canonical_type', 'source_chunk_id',
                'jurisdiction', 'source', 'license', 'refresh',
                'references_summary', 'title', 'text', 'entity', 'metadata',
            ]);
    }

    // ── pg_resource_potential_zone resolver ───────────────────────────────────

    public function test_pg_resource_potential_zone_resolver_returns_pgeo_envelope(): void
    {
        $entityRow = [
            'id' => self::PG_ID,
            'jurisdiction_code' => 'CA-SK',
            'source_id' => 'CA-SK-RESOURCE-POTENTIAL-GOLD',
            'source_feature_id' => '42',
            'commodity' => 'gold',
            'commodity_grouping' => 'precious_metals',
            'potential_rank' => 4,
            'methodology_ref' => 'GSC-OF-7x',
            'last_seen_at' => null,
        ];

        $this->mockPgeoResolverCall(
            entityTable: 'public_geoscience.pg_resource_potential_zone',
            entityRow: $entityRow,
            canonicalType: 'resource_potential_zone',
        );

        $chunkId = 'pg_resource_potential_zone:CA-SK-RESOURCE-POTENTIAL-GOLD:feature=42:pg_id='.self::PG_ID;

        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk()
            ->assertJsonPath('source_type', 'public_geoscience')
            ->assertJsonPath('canonical_type', 'resource_potential_zone')
            ->assertJsonStructure([
                'source_type', 'corpus', 'canonical_type', 'source_chunk_id',
                'jurisdiction', 'source', 'license', 'refresh',
                'references_summary', 'title', 'text', 'entity', 'metadata',
            ]);
    }

    // ── Carbon 3 staleness sign fix (Blocker #2) ──────────────────────────────

    public function test_staleness_seconds_is_positive_when_refreshed_30_minutes_ago(): void
    {
        $now = Carbon::create(2026, 4, 14, 12, 0, 0, 'UTC');
        Carbon::setTestNow($now);

        $thirtyMinsAgo = $now->copy()->subMinutes(30)->toDateTimeString();

        $this->mockPgeoResolverCall(
            entityTable: 'public_geoscience.pg_mine',
            entityRow: array_merge($this->fakeMineRow(), ['name' => 'Staleness Test Mine']),
            canonicalType: 'mine',
            lastRefreshedAt: $thirtyMinsAgo,
        );

        $chunkId = 'pg_mine:CA-SK-MINE-LOC:feature=12345:pg_id='.self::PG_ID;

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk();

        $staleness = $response->json('refresh.staleness_seconds');
        $this->assertIsInt($staleness, 'staleness_seconds must be an integer');
        $this->assertGreaterThan(0, $staleness,
            'staleness_seconds must be POSITIVE (Carbon 3 absolute: true required)');
        $this->assertEqualsWithDelta(1800, $staleness, 5,
            'staleness_seconds should be ~1800 for a 30-minute-old refresh');

        Carbon::setTestNow();
    }

    // ── references_summary — empty state ─────────────────────────────────────

    public function test_references_summary_count_is_zero_when_no_active_links(): void
    {
        $this->mockPgeoResolverCall(
            entityTable: 'public_geoscience.pg_mine',
            entityRow: $this->fakeMineRow(),
            canonicalType: 'mine',
        );

        $chunkId = 'pg_mine:CA-SK-MINE-LOC:feature=12345:pg_id='.self::PG_ID;

        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk()
            ->assertJsonPath('references_summary.count', 0)
            ->assertJsonPath('references_summary.documents', []);
    }

    // ── references_summary — with active links ────────────────────────────────

    public function test_references_summary_count_reflects_active_links(): void
    {
        $this->mockPgeoResolverCall(
            entityTable: 'public_geoscience.pg_mine',
            entityRow: $this->fakeMineRow(),
            canonicalType: 'mine',
            linkCount: 3,
        );

        $chunkId = 'pg_mine:CA-SK-MINE-LOC:feature=12345:pg_id='.self::PG_ID;

        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk()
            ->assertJsonPath('references_summary.count', 3);
    }

    // ── resolveReport — references_to_entities zero-fill ─────────────────────

    public function test_report_resolver_includes_zero_filled_references_to_entities(): void
    {
        $reportId = 'bbbbbbbb-0000-0000-0000-000000000002';

        $reportBuilder = \Mockery::mock('query_builder_report');
        $reportBuilder->shouldReceive('where')->withAnyArgs()->andReturn($reportBuilder);
        $reportBuilder->shouldReceive('first')->once()->andReturn((object) [
            'report_id' => $reportId,
            'title' => 'NI 43-101 Test Report',
            'company' => 'TestCo',
            'filing_date' => '2025-01-01',
            'commodity' => 'uranium',
            'sections_text' => json_encode(['1' => 'Section one text.']),
        ]);

        $linksBuilder = \Mockery::mock('query_builder_links');
        $linksBuilder->shouldReceive('where')->withAnyArgs()->andReturn($linksBuilder);
        $linksBuilder->shouldReceive('whereNull')->andReturn($linksBuilder);
        $linksBuilder->shouldReceive('select')->andReturn($linksBuilder);
        $linksBuilder->shouldReceive('groupBy')->andReturn($linksBuilder);
        $linksBuilder->shouldReceive('get')->andReturn(collect([]));

        // loadDocumentReferencesSummary calls DB::raw() on the facade (not the builder)
        // to build the COUNT(*) expression passed to select().
        DB::shouldReceive('raw')
            ->withAnyArgs()
            ->andReturnUsing(fn ($expr) => new Expression($expr));

        DB::shouldReceive('table')->with('silver.reports')->once()->andReturn($reportBuilder);
        DB::shouldReceive('table')->with('public_geoscience.document_entity_links')->andReturn($linksBuilder);

        $chunkId = "georag_reports:{$reportId}:section=1:chunk=abc";

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk();

        $refs = $response->json('references_to_entities');
        $this->assertSame(0, $refs['total']);
        $this->assertSame([], $refs['entities']);

        foreach (['mine', 'mineral_occurrence', 'drillhole_collar', 'resource_potential_zone'] as $t) {
            $this->assertArrayHasKey($t, $refs['by_canonical_type'],
                "Zero-fill key '{$t}' missing from references_to_entities.by_canonical_type");
            $this->assertSame(0, $refs['by_canonical_type'][$t]);
        }
    }

    // ── Regex widening (Concern #10): digits + underscores in canonical_type ──

    public function test_parse_accepts_digits_and_underscores_in_canonical_type(): void
    {
        $chunkId = 'pg_resource_potential_v2:CA-SK-RES:feature=99:pg_id='.self::PG_ID;

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id='.urlencode($chunkId))
            ->assertOk()
            ->assertJsonPath('source_type', 'unknown');

        $this->assertStringContainsString(
            'resource_potential_v2',
            $response->json('source_chunk_id'),
            'Regex must NOT strip digits/underscores — _v2 must survive the round-trip',
        );
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private function fakeMineRow(): array
    {
        return [
            'id' => self::PG_ID,
            'jurisdiction_code' => 'CA-SK',
            'source_id' => 'CA-SK-MINE-LOC',
            'source_feature_id' => '12345',
            'name' => 'Athabasca Test Mine',
            'status' => 'producing',
            'commodities' => '{uranium}',
            'commodity_grouping' => 'uranium',
            'operator' => 'TestCo Inc.',
            'source_url' => null,
            'last_seen_at' => null,
        ];
    }

    /**
     * Wire up all DB mocks for a complete PGEO resolver round-trip.
     *
     * The controller calls queries in this order:
     *   1. DB::table($entityTable)->where()->first()          — entity row
     *   2. DB::table('public_geoscience.sources as s')->...->first()  — source/jurisdiction
     *   3. DB::table('public_geoscience.document_entity_links')->...->count() — link count
     *   4. (when linkCount > 0) links detail query
     *
     * We return separate Mockery builder mocks per table so `first()` calls
     * don't cross-contaminate due to Mockery's shared-mock matching order.
     */
    private function mockPgeoResolverCall(
        string $entityTable,
        array $entityRow,
        string $canonicalType,
        ?string $lastRefreshedAt = null,
        int $linkCount = 0,
    ): void {
        $sourceObj = (object) [
            'source_id' => 'CA-SK-'.strtoupper(str_replace('_', '-', $canonicalType)),
            'source_name' => 'Test Source',
            'canonical_type' => $canonicalType,
            'service_url' => 'https://example.com/service',
            'license_summary' => 'Open Government Licence',
            'license_url' => 'https://example.com/license',
            'last_refreshed_at' => $lastRefreshedAt,
            'jurisdiction_code' => 'CA-SK',
            'jurisdiction_name' => 'Saskatchewan',
            'primary_authority' => 'Government of Saskatchewan',
        ];

        $entityObj = (object) $entityRow;

        // Mockery mock names must be valid PHP class-name strings (no dots/slashes).
        $entityBuilder = \Mockery::mock('entity_query_builder');
        $entityBuilder->shouldReceive('where')->withAnyArgs()->andReturn($entityBuilder);
        $entityBuilder->shouldReceive('first')->once()->andReturn($entityObj);

        $sourceBuilder = \Mockery::mock('source_query_builder');
        $sourceBuilder->shouldReceive('join')->withAnyArgs()->andReturn($sourceBuilder);
        $sourceBuilder->shouldReceive('where')->withAnyArgs()->andReturn($sourceBuilder);
        $sourceBuilder->shouldReceive('select')->andReturn($sourceBuilder);
        $sourceBuilder->shouldReceive('first')->once()->andReturn($sourceObj);

        $linksCountBuilder = \Mockery::mock('links_count_query_builder');
        $linksCountBuilder->shouldReceive('where')->withAnyArgs()->andReturn($linksCountBuilder);
        $linksCountBuilder->shouldReceive('whereNull')->andReturn($linksCountBuilder);
        $linksCountBuilder->shouldReceive('count')->andReturn($linkCount);

        DB::shouldReceive('table')->with($entityTable)->once()->andReturn($entityBuilder);
        DB::shouldReceive('table')->with('public_geoscience.sources as s')->once()->andReturn($sourceBuilder);
        DB::shouldReceive('table')->with('public_geoscience.document_entity_links')->andReturn($linksCountBuilder);

        if ($linkCount > 0) {
            $linksDetailBuilder = \Mockery::mock('links_detail_query_builder');
            $linksDetailBuilder->shouldReceive('leftJoin')->withAnyArgs()->andReturn($linksDetailBuilder);
            $linksDetailBuilder->shouldReceive('where')->withAnyArgs()->andReturn($linksDetailBuilder);
            $linksDetailBuilder->shouldReceive('whereNull')->andReturn($linksDetailBuilder);
            $linksDetailBuilder->shouldReceive('orderByDesc')->andReturn($linksDetailBuilder);
            $linksDetailBuilder->shouldReceive('limit')->andReturn($linksDetailBuilder);
            $linksDetailBuilder->shouldReceive('get')->andReturn(collect([]));

            DB::shouldReceive('table')
                ->with('public_geoscience.document_entity_links as l')
                ->andReturn($linksDetailBuilder);
        }
    }
}
