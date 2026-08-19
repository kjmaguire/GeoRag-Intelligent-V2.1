<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Feature tests for PublicGeoscience\PublicGeoscienceMapController.
 *
 * GET /api/v1/public-geoscience/map
 *
 * Postgres-only (like its sibling EntityReferencesControllerTest is
 * SQLite-mocked, but this controller's ST_X/ST_Y calls need a real
 * PostGIS geometry column — no equivalent to mock around). Seeds a real
 * pg_mine row against whichever jurisdiction/source the seed migration
 * (2026_05_13_180000_seed_public_geoscience_jurisdictions_and_sources)
 * already provisioned, rather than hardcoding values that migration
 * might not guarantee across environments.
 */
final class PublicGeoscienceMapControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    public function test_returns_401_without_auth(): void
    {
        $this->getJson('/api/v1/public-geoscience/map')->assertUnauthorized();
    }

    public function test_returns_a_feature_collection_with_a_seeded_mine(): void
    {
        $source = DB::table('public_geo.sources')->where('canonical_type', 'mine')->first();
        $this->assertNotNull($source, 'expected the seed migration to have provisioned at least one mine source');

        $mineId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO public_geo.pg_mine (
                id, jurisdiction_code, source_id, source_feature_id, name,
                source_crs, checksum, geom
             ) VALUES (
                ?::uuid, ?, ?, ?, ?,
                4326, ?, ST_SetSRID(ST_MakePoint(-106.3, 51.2), 4326)
             )',
            [
                $mineId, $source->jurisdiction_code, $source->source_id,
                'test-feature-'.$mineId, 'Test Mine',
                str_pad('1', 64, '0', STR_PAD_LEFT),
            ],
        );

        $user = User::factory()->create();

        $response = $this->actingAs($user)->getJson('/api/v1/public-geoscience/map');

        $response->assertOk();
        $response->assertJsonStructure(['type', 'feature_count', 'features']);
        $response->assertJsonFragment(['type' => 'FeatureCollection']);

        $body = $response->json();
        $mineFeature = collect($body['features'])->firstWhere('properties.id', $mineId);
        $this->assertNotNull($mineFeature, 'seeded mine should appear in the feature collection');
        $this->assertSame('Point', $mineFeature['geometry']['type']);
        $this->assertSame('mine', $mineFeature['properties']['layer']);
        $this->assertSame('Test Mine', $mineFeature['properties']['label']);
        $this->assertEqualsWithDelta(-106.3, $mineFeature['geometry']['coordinates'][0], 0.0001);
        $this->assertEqualsWithDelta(51.2, $mineFeature['geometry']['coordinates'][1], 0.0001);
    }

    public function test_jurisdiction_filter_excludes_other_jurisdictions(): void
    {
        $sources = DB::table('public_geo.sources')->where('canonical_type', 'mine')->get();
        if ($sources->count() < 2 || $sources->pluck('jurisdiction_code')->unique()->count() < 2) {
            $this->markTestSkipped('Need at least 2 mine sources in different jurisdictions from the seed migration.');
        }
        $byJurisdiction = $sources->unique('jurisdiction_code')->values();
        $sourceA = $byJurisdiction[0];
        $sourceB = $byJurisdiction[1];

        $mineAId = (string) Str::uuid();
        $mineBId = (string) Str::uuid();
        foreach ([[$mineAId, $sourceA], [$mineBId, $sourceB]] as [$id, $source]) {
            DB::statement(
                'INSERT INTO public_geo.pg_mine (
                    id, jurisdiction_code, source_id, source_feature_id, name,
                    source_crs, checksum, geom
                 ) VALUES (
                    ?::uuid, ?, ?, ?, ?,
                    4326, ?, ST_SetSRID(ST_MakePoint(-106.3, 51.2), 4326)
                 )',
                [
                    $id, $source->jurisdiction_code, $source->source_id,
                    'test-feature-'.$id, 'Jurisdiction Filter Test',
                    str_pad('1', 64, '0', STR_PAD_LEFT),
                ],
            );
        }

        $user = User::factory()->create();
        $response = $this->actingAs($user)
            ->getJson('/api/v1/public-geoscience/map?jurisdiction='.$sourceA->jurisdiction_code);

        $response->assertOk();
        $ids = collect($response->json('features'))->pluck('properties.id');
        $this->assertTrue($ids->contains($mineAId));
        $this->assertFalse($ids->contains($mineBId));
    }

    /**
     * Bulk-seed N mines in a tight lng/lat band via generate_series.
     *
     * Row-at-a-time inserts of 4k+ rows dominate the test's runtime; one
     * set-returning INSERT keeps it under a second.
     */
    private function seedBulkMines(object $source, int $count, float $lngBase = -120.0, float $latBase = 50.0): void
    {
        DB::statement(
            'INSERT INTO public_geo.pg_mine (
                id, jurisdiction_code, source_id, source_feature_id, name,
                source_crs, checksum, geom
             )
             SELECT gen_random_uuid(), ?, ?, ?||g, ?||g, 4326,
                    lpad(md5(?||g::text), 64, ?),
                    ST_SetSRID(ST_MakePoint(? + (g % 100) * 0.002, ? + (g / 100) * 0.002), 4326)
               FROM generate_series(1, ?) g',
            [
                $source->jurisdiction_code, $source->source_id,
                'bulk-feature-', 'Bulk Mine ', 'seed-', '0',
                $lngBase, $latBase, $count,
            ],
        );
    }

    private function mineSource(): object
    {
        $source = DB::table('public_geo.sources')->where('canonical_type', 'mine')->first();
        $this->assertNotNull($source, 'expected the seed migration to have provisioned at least one mine source');

        return $source;
    }

    public function test_bbox_excludes_points_outside_the_viewport(): void
    {
        $source = $this->mineSource();

        $inside = (string) Str::uuid();
        $outside = (string) Str::uuid();
        foreach ([[$inside, -106.3, 51.2], [$outside, 10.0, 10.0]] as [$id, $lng, $lat]) {
            DB::statement(
                'INSERT INTO public_geo.pg_mine (
                    id, jurisdiction_code, source_id, source_feature_id, name,
                    source_crs, checksum, geom
                 ) VALUES (?::uuid, ?, ?, ?, ?, 4326, ?, ST_SetSRID(ST_MakePoint(?, ?), 4326))',
                [
                    $id, $source->jurisdiction_code, $source->source_id,
                    'bbox-feature-'.$id, 'BBox Test', str_pad('2', 64, '0', STR_PAD_LEFT),
                    $lng, $lat,
                ],
            );
        }

        $response = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-110,48,-100,55');

        $response->assertOk();
        $ids = collect($response->json('features'))->pluck('properties.id');
        $this->assertTrue($ids->contains($inside), 'point inside the bbox should be returned');
        $this->assertFalse($ids->contains($outside), 'point outside the bbox must be excluded');
    }

    /**
     * The regression this whole rewrite exists for.
     *
     * The previous controller capped at MAX_ROWS_PER_TABLE = 2000 and
     * returned an arbitrary subset with no signal — against the real corpus
     * (412,537 mineral occurrences) that silently hid 99.5% of a layer.
     * Above the point ceiling the response must aggregate rather than
     * truncate, and the cluster point_counts must sum to EVERY seeded row.
     */
    public function test_dense_layer_clusters_instead_of_silently_truncating(): void
    {
        $source = $this->mineSource();
        $seeded = 4100; // > MAX_POINTS_PER_LAYER (4000)
        $this->seedBulkMines($source, $seeded);

        $response = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-125,45,-115,55&zoom=6');

        $response->assertOk();
        $body = $response->json();

        $this->assertSame('clustered', $body['modes']['mine'] ?? null);
        $this->assertGreaterThanOrEqual($seeded, $body['total_in_view']);

        $clusters = collect($body['features'])->where('properties.layer', 'mine');
        $this->assertTrue($clusters->every(fn ($f) => $f['properties']['cluster'] === true));

        // Nothing dropped: the cells account for every seeded row.
        $this->assertSame($seeded, (int) $clusters->sum('properties.point_count'));

        // And the wire payload is far smaller than the row count — that is
        // the point of aggregating rather than shipping 4,100 features.
        $this->assertLessThan($seeded, $clusters->count());
    }

    public function test_sparse_layer_returns_individual_points_with_labels(): void
    {
        $source = $this->mineSource();
        $this->seedBulkMines($source, 50);

        $response = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-125,45,-115,55&zoom=6');

        $response->assertOk();
        $body = $response->json();

        $this->assertSame('points', $body['modes']['mine'] ?? null);
        $this->assertFalse($body['truncated']);

        $mines = collect($body['features'])->where('properties.layer', 'mine');
        $this->assertSame(50, $mines->count());
        $this->assertTrue($mines->every(fn ($f) => $f['properties']['cluster'] === false));
        $this->assertNotNull($mines->first()['properties']['label']);
    }

    public function test_total_in_view_is_the_true_count_not_the_feature_count(): void
    {
        $source = $this->mineSource();
        $seeded = 4100;
        $this->seedBulkMines($source, $seeded);

        $response = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-125,45,-115,55&zoom=6');

        $response->assertOk();
        $body = $response->json();

        // The two numbers must differ, and the UI is expected to report the
        // former. If these ever became equal the clustering silently stopped.
        $this->assertGreaterThanOrEqual($seeded, $body['total_in_view']);
        $this->assertLessThan($body['total_in_view'], $body['feature_count']);
    }

    public function test_reversed_bbox_corners_are_tolerated(): void
    {
        $source = $this->mineSource();
        $this->seedBulkMines($source, 10);

        // max/min handed over in the wrong order — must not silently match zero rows.
        $response = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-115,55,-125,45');

        $response->assertOk();
        $this->assertGreaterThan(0, $response->json('total_in_view'));
    }

    public function test_rejects_a_malformed_bbox(): void
    {
        $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=nonsense')
            ->assertStatus(422);
    }

    public function test_rejects_an_out_of_range_zoom(): void
    {
        $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?zoom=99')
            ->assertStatus(422);
    }
}
