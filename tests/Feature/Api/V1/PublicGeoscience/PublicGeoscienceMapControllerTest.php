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
}
