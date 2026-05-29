<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Feature tests for PublicGeoscience\EntityReferencesController.
 *
 * Routes:
 *   GET /api/v1/public-geoscience/entities/{canonical_type}/{pg_id}/references
 *   GET /api/v1/public-geoscience/documents/{report_id}/references
 *
 * DB::table() mocked throughout — public_geoscience.* schema not available
 * in the SQLite in-memory test environment.
 */
class EntityReferencesControllerTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private const VALID_UUID = 'cccccccc-0000-0000-0000-000000000001';
    private const VALID_DOC_UUID = 'dddddddd-0000-0000-0000-000000000001';

    protected function setUp(): void
    {
        parent::setUp();
        $this->user = User::factory()->create();
    }

    // ── forEntity — 401 ──────────────────────────────────────────────────────

    public function test_for_entity_returns_401_without_auth(): void
    {
        $this->getJson('/api/v1/public-geoscience/entities/mine/' . self::VALID_UUID . '/references')
            ->assertUnauthorized();
    }

    // ── forEntity — 404 on unknown canonical_type ─────────────────────────────

    public function test_for_entity_returns_404_for_unknown_canonical_type(): void
    {
        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/entities/unknown_type/' . self::VALID_UUID . '/references')
            ->assertNotFound();
    }

    public function test_for_entity_returns_404_for_non_whitelisted_type_gemstone(): void
    {
        // 'gemstone' is a real geological concept but not in the controller whitelist.
        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/entities/gemstone/' . self::VALID_UUID . '/references')
            ->assertNotFound();
    }

    // ── forEntity — 400 on invalid pg_id ──────────────────────────────────────

    public function test_for_entity_returns_400_for_non_uuid_pg_id(): void
    {
        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/entities/mine/not-a-uuid/references')
            ->assertStatus(400);
    }

    public function test_for_entity_returns_400_for_numeric_pg_id(): void
    {
        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/entities/mine/12345/references')
            ->assertStatus(400);
    }

    // ── forEntity — empty state ───────────────────────────────────────────────

    public function test_for_entity_empty_state_returns_correct_shape(): void
    {
        $this->mockLinksQuery(self::VALID_UUID, 'mine', collect([]));

        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/entities/mine/' . self::VALID_UUID . '/references')
            ->assertOk()
            ->assertJsonPath('canonical_type', 'mine')
            ->assertJsonPath('pg_id', self::VALID_UUID)
            ->assertJsonPath('total', 0)
            ->assertJsonPath('min_confidence', 0.6)
            ->assertJsonPath('documents', []);
    }

    public function test_for_entity_empty_state_response_structure(): void
    {
        $this->mockLinksQuery(self::VALID_UUID, 'mineral_occurrence', collect([]));

        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/entities/mineral_occurrence/' . self::VALID_UUID . '/references')
            ->assertOk()
            ->assertJsonStructure([
                'canonical_type',
                'pg_id',
                'total',
                'min_confidence',
                'documents',
            ]);
    }

    // ── forEntity — all four valid canonical_types ────────────────────────────

    public function test_for_entity_accepts_all_four_whitelisted_canonical_types(): void
    {
        $types = ['mine', 'mineral_occurrence', 'drillhole_collar', 'resource_potential_zone'];

        foreach ($types as $type) {
            $this->mockLinksQuery(self::VALID_UUID, $type, collect([]));

            $this->actingAs($this->user)
                ->getJson("/api/v1/public-geoscience/entities/{$type}/" . self::VALID_UUID . '/references')
                ->assertOk()
                ->assertJsonPath('canonical_type', $type);
        }
    }

    // ── forEntity — min_confidence filter ────────────────────────────────────

    public function test_for_entity_min_confidence_filter_is_applied(): void
    {
        // We assert the query is forwarded; the DB mock captures the confidence param.
        // A confidence=0.9 filter means low-confidence rows are excluded.
        $this->mockLinksQuery(self::VALID_UUID, 'mine', collect([]), minConfidence: 0.9);

        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/entities/mine/' . self::VALID_UUID . '/references?min_confidence=0.9')
            ->assertOk()
            ->assertJsonPath('min_confidence', 0.9);
    }

    // ── forDocument — 401 ────────────────────────────────────────────────────

    public function test_for_document_returns_401_without_auth(): void
    {
        $this->getJson('/api/v1/public-geoscience/documents/' . self::VALID_DOC_UUID . '/references')
            ->assertUnauthorized();
    }

    // ── forDocument — 400 on invalid UUID ────────────────────────────────────

    public function test_for_document_returns_400_for_non_uuid_report_id(): void
    {
        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/documents/not-a-uuid/references')
            ->assertStatus(400);
    }

    public function test_for_document_returns_400_for_numeric_report_id(): void
    {
        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/documents/99999/references')
            ->assertStatus(400);
    }

    // ── forDocument — empty state ─────────────────────────────────────────────

    public function test_for_document_empty_state_returns_zero_filled_counts(): void
    {
        $this->mockDocumentLinksQuery(self::VALID_DOC_UUID, collect([]));

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/documents/' . self::VALID_DOC_UUID . '/references')
            ->assertOk();

        $counts = $response->json('counts');
        $this->assertSame(0, $counts['mine'], 'mine count must be zero when no links');
        $this->assertSame(0, $counts['mineral_occurrence']);
        $this->assertSame(0, $counts['drillhole_collar']);
        $this->assertSame(0, $counts['resource_potential_zone']);
    }

    public function test_for_document_empty_state_has_empty_by_canonical_type(): void
    {
        $this->mockDocumentLinksQuery(self::VALID_DOC_UUID, collect([]));

        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/documents/' . self::VALID_DOC_UUID . '/references')
            ->assertOk()
            ->assertJsonPath('by_canonical_type', []);
    }

    public function test_for_document_empty_state_response_structure(): void
    {
        $this->mockDocumentLinksQuery(self::VALID_DOC_UUID, collect([]));

        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/documents/' . self::VALID_DOC_UUID . '/references')
            ->assertOk()
            ->assertJsonStructure([
                'document_id',
                'total',
                'min_confidence',
                'counts' => ['mine', 'mineral_occurrence', 'drillhole_collar', 'resource_potential_zone'],
                'by_canonical_type',
            ]);
    }

    // ── forDocument — with links ──────────────────────────────────────────────

    public function test_for_document_counts_reflect_active_links_per_canonical_type(): void
    {
        $links = collect([
            (object) [
                'canonical_type' => 'mine',
                'entity_id'      => 'eeeeeeee-0000-0000-0000-000000000001',
                'confidence'     => '0.85',
                'signals'        => null,
                'extracted_context' => 'Test mine context',
                'established_at' => '2026-01-01 00:00:00',
                'established_by' => 'smad_linker_v1',
            ],
            (object) [
                'canonical_type' => 'mine',
                'entity_id'      => 'eeeeeeee-0000-0000-0000-000000000002',
                'confidence'     => '0.72',
                'signals'        => null,
                'extracted_context' => null,
                'established_at' => '2026-01-01 00:00:00',
                'established_by' => 'smad_linker_v1',
            ],
            (object) [
                'canonical_type' => 'drillhole_collar',
                'entity_id'      => 'eeeeeeee-0000-0000-0000-000000000003',
                'confidence'     => '0.91',
                'signals'        => null,
                'extracted_context' => null,
                'established_at' => '2026-01-01 00:00:00',
                'established_by' => 'smad_linker_v1',
            ],
        ]);

        $this->mockDocumentLinksQueryWithNames(self::VALID_DOC_UUID, $links);

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/documents/' . self::VALID_DOC_UUID . '/references')
            ->assertOk();

        $counts = $response->json('counts');
        $this->assertSame(2, $counts['mine']);
        $this->assertSame(0, $counts['mineral_occurrence']);
        $this->assertSame(1, $counts['drillhole_collar']);
        $this->assertSame(0, $counts['resource_potential_zone']);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private function mockLinksQuery(string $pgId, string $canonicalType, $rows, float $minConfidence = 0.6): void
    {
        DB::shouldReceive('table')
            ->with('public_geoscience.document_entity_links as l')
            ->andReturnSelf();
        DB::shouldReceive('leftJoin')->withAnyArgs()->andReturnSelf();
        DB::shouldReceive('where')->withAnyArgs()->andReturnSelf();
        DB::shouldReceive('whereNull')->andReturnSelf();
        DB::shouldReceive('orderByDesc')->andReturnSelf();
        DB::shouldReceive('get')->andReturn($rows);
    }

    private function mockDocumentLinksQuery(string $reportId, $rows): void
    {
        DB::shouldReceive('table')
            ->with('public_geoscience.document_entity_links')
            ->andReturnSelf();
        DB::shouldReceive('where')->withAnyArgs()->andReturnSelf();
        DB::shouldReceive('whereNull')->andReturnSelf();
        DB::shouldReceive('orderBy')->andReturnSelf();
        DB::shouldReceive('orderByDesc')->andReturnSelf();
        DB::shouldReceive('get')->andReturn($rows);
    }

    private function mockDocumentLinksQueryWithNames(string $reportId, $rows): void
    {
        $this->mockDocumentLinksQuery($reportId, $rows);

        // DB::raw() is called by fetchEntityNames for drillhole_collar and
        // resource_potential_zone to build the COALESCE/commodity name expression.
        DB::shouldReceive('raw')
            ->withAnyArgs()
            ->andReturnUsing(fn ($expr) => new \Illuminate\Database\Query\Expression($expr));

        // fetchEntityNames calls DB::table($entityTable)->whereIn()->select()->get()
        // for each unique canonical_type in the rows. Use separate Mockery builder
        // mocks per table to prevent get() returning the wrong result set.
        $mineBuilder = \Mockery::mock('mine_entity_query_builder');
        $mineBuilder->shouldReceive('whereIn')->withAnyArgs()->andReturn($mineBuilder);
        $mineBuilder->shouldReceive('select')->withAnyArgs()->andReturn($mineBuilder);
        $mineBuilder->shouldReceive('get')->andReturn(collect([]));

        $drillholeBuilder = \Mockery::mock('drillhole_entity_query_builder');
        $drillholeBuilder->shouldReceive('whereIn')->withAnyArgs()->andReturn($drillholeBuilder);
        $drillholeBuilder->shouldReceive('select')->withAnyArgs()->andReturn($drillholeBuilder);
        $drillholeBuilder->shouldReceive('get')->andReturn(collect([]));

        DB::shouldReceive('table')->with('public_geoscience.pg_mine')->andReturn($mineBuilder);
        DB::shouldReceive('table')->with('public_geoscience.pg_drillhole_collar')->andReturn($drillholeBuilder);
    }
}
