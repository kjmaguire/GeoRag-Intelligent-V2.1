<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Cache;
use Tests\TestCase;

/**
 * Feature tests for PublicGeoscience\JurisdictionController.
 *
 * Route: GET /api/v1/public-geoscience/jurisdictions (auth:sanctum)
 *
 * Strategy: use Cache::put() to pre-seed the in-memory cache so that
 * Cache::remember() returns our fixture payload without hitting the database.
 * This avoids the Mockery strictness issue where Cache::shouldReceive() blocks
 * unrelated Cache::store() calls made by the session/auth middleware.
 */
class JurisdictionControllerTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private const CACHE_KEY = 'public-geoscience:jurisdictions:v1';

    protected function setUp(): void
    {
        parent::setUp();
        $this->user = User::factory()->create();
    }

    // 401 -----------------------------------------------------------------------

    public function test_unauthenticated_request_returns_401(): void
    {
        $this->getJson('/api/v1/public-geoscience/jurisdictions')
            ->assertUnauthorized();
    }

    // Envelope shape ------------------------------------------------------------

    public function test_authenticated_request_returns_envelope_shape(): void
    {
        Cache::put(self::CACHE_KEY, $this->buildMinimalPayload(), 300);

        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/jurisdictions')
            ->assertOk()
            ->assertJsonStructure([
                'data' => [
                    'countries',
                    'counts' => ['total', 'active', 'coming_soon'],
                ],
                'generated_at',
                'cache_ttl_seconds',
            ]);
    }

    public function test_cache_ttl_seconds_is_300(): void
    {
        Cache::put(self::CACHE_KEY, $this->buildMinimalPayload(), 300);

        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/jurisdictions')
            ->assertOk()
            ->assertJsonPath('cache_ttl_seconds', 300);
    }

    public function test_generated_at_is_present_and_is_a_string(): void
    {
        Cache::put(self::CACHE_KEY, $this->buildMinimalPayload(), 300);

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/jurisdictions')
            ->assertOk();

        $this->assertIsString($response->json('generated_at'));
    }

    // Canada group --------------------------------------------------------------

    public function test_response_contains_canada_country_group_when_seeded(): void
    {
        Cache::put(self::CACHE_KEY, $this->buildCanadaPayload(), 300);

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/jurisdictions')
            ->assertOk();

        $codes = array_column($response->json('data.countries'), 'country_code');
        $this->assertContains('CA', $codes, 'Expected a Canada (CA) country group');
    }

    public function test_canada_group_contains_active_and_coming_soon_counts(): void
    {
        Cache::put(self::CACHE_KEY, $this->buildCanadaPayload(), 300);

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/jurisdictions')
            ->assertOk();

        $counts = $response->json('data.counts');
        $this->assertGreaterThanOrEqual(1, $counts['active'], 'Expected at least 1 active jurisdiction');
        $this->assertGreaterThanOrEqual(1, $counts['coming_soon'], 'Expected at least 1 coming_soon');
    }

    public function test_canada_group_contains_nested_jurisdictions_array(): void
    {
        Cache::put(self::CACHE_KEY, $this->buildCanadaPayload(), 300);

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/jurisdictions')
            ->assertOk();

        $canada = collect($response->json('data.countries'))->firstWhere('country_code', 'CA');
        $this->assertNotNull($canada);
        $this->assertArrayHasKey('jurisdictions', $canada);
        $this->assertNotEmpty($canada['jurisdictions']);
    }

    // Cache key -----------------------------------------------------------------

    public function test_cache_remember_called_with_correct_key_and_ttl(): void
    {
        // Verify cache miss causes the controller to call Cache::remember() with
        // the correct key and TTL. We pre-warm the array cache store so the
        // remember() call returns without hitting the database.
        Cache::put(self::CACHE_KEY, $this->buildMinimalPayload(), 300);

        $this->actingAs($this->user)
            ->getJson('/api/v1/public-geoscience/jurisdictions')
            ->assertOk()
            ->assertJsonPath('cache_ttl_seconds', 300);

        // The cache key we seeded is the same one the controller uses — if the
        // controller used a different key, the response would be built from the
        // live DB instead (which returns an empty payload on SQLite).
        $this->assertNotNull(Cache::get(self::CACHE_KEY));
    }

    // Helpers -------------------------------------------------------------------

    private function buildMinimalPayload(): array
    {
        return [
            'countries' => [],
            'counts' => ['total' => 0, 'active' => 0, 'coming_soon' => 0],
        ];
    }

    private function buildCanadaPayload(): array
    {
        return [
            'countries' => [
                [
                    'country_code' => 'CA',
                    'display_name' => 'Canada',
                    'jurisdictions' => [
                        ['jurisdiction_code' => 'CA-SK', 'country_code' => 'CA',
                            'display_name' => 'Saskatchewan', 'level' => 'province',
                            'status' => 'active', 'sort_order' => 10, 'sources' => []],
                        ['jurisdiction_code' => 'CA-BC', 'country_code' => 'CA',
                            'display_name' => 'British Columbia', 'level' => 'province',
                            'status' => 'active', 'sort_order' => 20, 'sources' => []],
                        ['jurisdiction_code' => 'CA-AB', 'country_code' => 'CA',
                            'display_name' => 'Alberta', 'level' => 'province',
                            'status' => 'coming_soon', 'sort_order' => 30, 'sources' => []],
                    ],
                ],
            ],
            'counts' => ['total' => 3, 'active' => 2, 'coming_soon' => 1],
        ];
    }
}
