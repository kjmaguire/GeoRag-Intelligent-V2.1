<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

/**
 * Feature tests for PublicGeoscience\TileProxyController.
 *
 * Route: GET /tiles/public-geoscience/{source}/{z}/{x}/{y}.pbf
 *        (auth:sanctum — defined in routes/web.php)
 *
 * The controller proxies tile requests to an internal Martin instance.
 * Http::fake() intercepts the upstream call so no real network is needed.
 */
class TileProxyControllerTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    /** A valid source from the controller whitelist. */
    private const VALID_SOURCE = 'pg_mines';

    protected function setUp(): void
    {
        parent::setUp();
        $this->user = User::factory()->create();
    }

    // ── 401 — unauthenticated ─────────────────────────────────────────────────

    public function test_unauthenticated_request_returns_401(): void
    {
        $this->getJson('/tiles/public-geoscience/' . self::VALID_SOURCE . '/10/123/456.pbf')
            ->assertUnauthorized();
    }

    // ── 404 — unknown source ──────────────────────────────────────────────────

    public function test_unknown_source_returns_404(): void
    {
        $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/silver_collars/10/123/456.pbf')
            ->assertNotFound();
    }

    public function test_empty_source_segment_returns_404(): void
    {
        // 'pg_project' is not in the whitelist — would be a SSRF attempt.
        $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/pg_project/10/123/456.pbf')
            ->assertNotFound();
    }

    // ── 400 — invalid tile coordinates ────────────────────────────────────────
    //
    // The route regex WHERE clause only allows [0-9]+, so negative integers
    // won't match the route pattern — Laravel returns 404 for those (the
    // route simply doesn't match). The controller's own 400 guard is
    // reachable if z > 24 for example, but the route regex also pre-filters
    // that. We test what the controller itself can check via its guard.

    public function test_negative_x_returns_400_or_route_not_matched(): void
    {
        // Negative tile x: the route regex [0-9]+ won't match '-1', so the
        // route won't be found at all (404). Either behaviour is acceptable —
        // the important thing is it does NOT return 200 with a tile body.
        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/' . self::VALID_SOURCE . '/10/-1/456.pbf');

        $this->assertContains($response->status(), [400, 404],
            'Negative tile x must yield 400 or 404, never 200');
    }

    public function test_negative_y_returns_400_or_route_not_matched(): void
    {
        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/' . self::VALID_SOURCE . '/10/123/-1.pbf');

        $this->assertContains($response->status(), [400, 404],
            'Negative tile y must yield 400 or 404, never 200');
    }

    // ── Happy path — 200 with correct headers ────────────────────────────────

    public function test_valid_tile_request_returns_200_with_protobuf_content_type(): void
    {
        Http::fake([
            '*' => Http::response(
                'fake-protobuf-bytes',
                200,
                ['Content-Type' => 'application/x-protobuf'],
            ),
        ]);

        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/' . self::VALID_SOURCE . '/10/123/456.pbf');

        $response->assertOk();
        $this->assertStringContainsString(
            'application/x-protobuf',
            $response->headers->get('Content-Type'),
        );
    }

    public function test_valid_tile_request_sets_cache_control_header(): void
    {
        Http::fake([
            '*' => Http::response(
                'fake-protobuf-bytes',
                200,
                ['Content-Type' => 'application/x-protobuf'],
            ),
        ]);

        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/' . self::VALID_SOURCE . '/10/123/456.pbf');

        $response->assertOk();
        // Symfony's ResponseHeaderBag normalises Cache-Control to the canonical
        // RFC 7234 order: directives appear as "max-age=N, public". Assert on
        // the actual normalised form rather than assuming directive order.
        $this->assertSame(
            'max-age=300, public',
            $response->headers->get('Cache-Control'),
        );
    }

    public function test_valid_tile_request_returns_tile_body(): void
    {
        $fakeBytes = 'binary-tile-body';

        Http::fake([
            '*' => Http::response($fakeBytes, 200, ['Content-Type' => 'application/x-protobuf']),
        ]);

        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/' . self::VALID_SOURCE . '/10/123/456.pbf');

        $response->assertOk();
        $this->assertSame($fakeBytes, $response->getContent());
    }

    // ── All four whitelisted sources ──────────────────────────────────────────

    public function test_all_whitelisted_sources_are_accepted(): void
    {
        $sources = ['pg_mines', 'pg_mineral_occurrences', 'pg_drillhole_collars', 'pg_resource_potential'];

        foreach ($sources as $source) {
            Http::fake([
                '*' => Http::response('tile', 200, ['Content-Type' => 'application/x-protobuf']),
            ]);

            $this->actingAs($this->user)
                ->get("/tiles/public-geoscience/{$source}/5/10/15.pbf")
                ->assertOk();
        }
    }

    // ── 204 passthrough from Martin ───────────────────────────────────────────

    public function test_martin_204_no_content_is_passed_through(): void
    {
        Http::fake([
            '*' => Http::response('', 204),
        ]);

        $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/' . self::VALID_SOURCE . '/10/123/456.pbf')
            ->assertNoContent();
    }

    // ── 502 when upstream Martin throws ──────────────────────────────────────

    public function test_upstream_martin_exception_returns_502(): void
    {
        Http::fake([
            '*' => function () {
                throw new \Illuminate\Http\Client\ConnectionException('Martin is down');
            },
        ]);

        $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/' . self::VALID_SOURCE . '/10/123/456.pbf')
            ->assertStatus(502);
    }
}
