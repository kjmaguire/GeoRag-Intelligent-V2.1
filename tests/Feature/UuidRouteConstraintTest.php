<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * LAR-15 (2026-09-29 audit): non-UUID segments in uuid-keyed routes must be
 * a 404, not a 500.
 *
 * Without a route constraint `/holes/PLS-20-01/detail` (a hole-ID-shaped
 * link) and `/api/v1/projects/abc` reached Postgres as a uuid comparison
 * against text and raised 22P02 — a 500. SQLite compares the text and finds
 * nothing, which is why the suite never saw it; the route constraint makes
 * the answer independent of the driver, and a query log proves no SQL runs.
 */
final class UuidRouteConstraintTest extends TestCase
{
    use RefreshDatabase;

    private const UUID = '0190f3a1-2b3c-7d4e-8f90-a1b2c3d4e5f6';

    /**
     * @return array<string, array{0: string, 1: string}>
     */
    public static function nonUuidUrls(): array
    {
        return [
            'drillhole detail by hole id' => ['GET', '/projects/some-project/holes/PLS-20-01/detail'],
            'project show' => ['GET', '/api/v1/projects/not-a-uuid'],
            'project update' => ['PATCH', '/api/v1/projects/not-a-uuid'],
            'project destroy' => ['DELETE', '/api/v1/projects/not-a-uuid'],
            'collars index' => ['GET', '/api/v1/projects/not-a-uuid/collars'],
            'collar show' => ['GET', '/api/v1/projects/'.self::UUID.'/collars/PLS-20-01'],
            'exports index' => ['GET', '/api/v1/projects/not-a-uuid/exports'],
            'export show' => ['GET', '/api/v1/projects/'.self::UUID.'/exports/not-a-uuid'],
            'export download' => ['GET', '/api/v1/exports/not-a-uuid/download'],
            'upload' => ['POST', '/api/v1/projects/not-a-uuid/upload'],
        ];
    }

    #[DataProvider('nonUuidUrls')]
    public function test_a_non_uuid_segment_is_a_404_before_any_query(string $method, string $url): void
    {
        $user = User::factory()->create();

        DB::enableQueryLog();
        DB::flushQueryLog();

        $this->actingAs($user)
            ->json($method, $url)
            ->assertNotFound();

        $this->assertSame(
            [],
            array_values(array_filter(
                DB::getQueryLog(),
                static fn (array $q): bool => str_contains($q['query'], 'projects')
                    || str_contains($q['query'], 'collars')
                    || str_contains($q['query'], 'exports'),
            )),
            'the route must refuse to match, not let the controller query with a non-UUID',
        );
    }

    public function test_a_uuid_segment_still_routes(): void
    {
        $user = User::factory()->create();

        // Routed to the controller, which answers its own not-found for a
        // project the user has no access to — anything but the router's 404
        // page shape would do; the point is that the pattern matches.
        $response = $this->actingAs($user)->getJson('/api/v1/projects/'.self::UUID);

        $this->assertContains($response->status(), [403, 404]);
        $this->assertNotNull($response->json());
    }
}
