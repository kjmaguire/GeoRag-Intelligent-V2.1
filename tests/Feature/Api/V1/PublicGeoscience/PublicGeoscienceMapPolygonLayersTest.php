<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Http\Controllers\Api\V1\PublicGeoscience\PublicGeoscienceMapController;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Polygon layers on GET /api/v1/public-geoscience/map (`layers=`), plus the
 * per-layer SQL behind GET .../sync-status.
 *
 * Postgres-only: ST_ClipByBox2D / ST_SimplifyPreserveTopology / ST_AsGeoJSON
 * need PostGIS. Seeds against the CA-SK disposition source the 2026-08-19
 * registry seed provisions.
 */
final class PublicGeoscienceMapPolygonLayersTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private function dispositionSource(): object
    {
        $source = DB::table('public_geo.sources')
            ->where('source_id', 'CA-SK-MINERAL-DISPOSITION-MINING-0')
            ->first();
        $this->assertNotNull($source, 'expected the registry seed to provision CA-SK-MINERAL-DISPOSITION-MINING-0');

        return $source;
    }

    /**
     * A square disposition parcel of side $size degrees at ($lng, $lat).
     */
    private function seedParcel(float $lng, float $lat, float $size = 0.01, string $number = 'MC-1'): string
    {
        $source = $this->dispositionSource();
        $id = (string) Str::uuid();
        DB::statement(
            "INSERT INTO public_geo.pg_mineral_disposition (
                id, jurisdiction_code, source_id, source_feature_id, disposition_number,
                disposition_type, status, holder_name, area_ha, source_crs, checksum, geom
             ) VALUES (
                ?::uuid, ?, ?, ?, ?, 'mineral', 'active', 'Acme Exploration', 100, 4326, ?,
                ST_Multi(ST_MakeEnvelope(?, ?, ?, ?, 4326))
             )",
            [
                $id, $source->jurisdiction_code, $source->source_id, 'poly-'.$id, $number,
                str_pad('3', 64, '0', STR_PAD_LEFT),
                $lng, $lat, $lng + $size, $lat + $size,
            ],
        );

        return $id;
    }

    public function test_no_layers_param_keeps_the_point_only_response(): void
    {
        $this->seedParcel(-105.5, 57.5);

        $body = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-106,57,-105,58&zoom=9')
            ->assertOk()
            ->json();

        $this->assertArrayNotHasKey('polygons', $body);
        $this->assertArrayNotHasKey('polygon_layers', $body);
    }

    public function test_tenure_polygon_in_view_is_returned_with_attributes_and_licence(): void
    {
        $inside = $this->seedParcel(-105.5, 57.5, 0.01, 'MC-IN');
        $outside = $this->seedParcel(-90.0, 50.0, 0.01, 'MC-OUT');

        $body = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-106,57,-105,58&zoom=9&layers=mineral_disposition')
            ->assertOk()
            ->json();

        $ids = collect($body['polygons']['features'])->pluck('properties.id');
        $this->assertTrue($ids->contains($inside));
        $this->assertFalse($ids->contains($outside));

        $feature = collect($body['polygons']['features'])->firstWhere('properties.id', $inside);
        $this->assertContains($feature['geometry']['type'], ['Polygon', 'MultiPolygon']);
        $this->assertSame('mineral_disposition', $feature['properties']['layer']);
        $this->assertSame('MC-IN', $feature['properties']['label']);
        $this->assertSame('Acme Exploration', $feature['properties']['holder_name']);
        $this->assertSame('active', $feature['properties']['status']);

        $meta = $body['polygon_layers']['mineral_disposition'];
        $this->assertSame('polygons', $meta['mode']);
        $this->assertSame(1, $meta['total_in_view']);
        $this->assertFalse($meta['truncated']);

        $this->assertArrayHasKey('CA-SK-MINERAL-DISPOSITION-MINING-0', $body['sources']);
        $this->assertNotEmpty($body['sources']['CA-SK-MINERAL-DISPOSITION-MINING-0']['license_summary']);

        // Points are untouched by the polygon request.
        $this->assertSame(0, $body['total_in_view']);
    }

    public function test_below_min_zoom_nothing_is_fetched_and_the_ui_is_told(): void
    {
        $this->seedParcel(-105.5, 57.5);

        $body = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-110,50,-100,60&zoom=4&layers=mineral_disposition')
            ->assertOk()
            ->json();

        $this->assertSame([], $body['polygons']['features']);
        $this->assertSame('min_zoom', $body['polygon_layers']['mineral_disposition']['mode']);
        $this->assertEquals(6.0, $body['polygon_layers']['mineral_disposition']['min_zoom']);
    }

    public function test_province_wide_request_is_capped_and_says_so(): void
    {
        $source = $this->dispositionSource();
        $count = PublicGeoscienceMapController::MAX_POLYGONS_PER_LAYER + 50;
        DB::statement(
            "INSERT INTO public_geo.pg_mineral_disposition (
                id, jurisdiction_code, source_id, source_feature_id, disposition_number,
                disposition_type, status, source_crs, checksum, geom
             )
             SELECT gen_random_uuid(), ?, ?, 'bulk-'||g, 'B-'||g, 'mineral', 'active', 4326,
                    lpad(md5('b'||g::text), 64, '0'),
                    ST_Multi(ST_MakeEnvelope(-109 + (g % 60) * 0.1, 50 + (g / 60) * 0.1,
                                             -109 + (g % 60) * 0.1 + 0.05, 50 + (g / 60) * 0.1 + 0.05, 4326))
               FROM generate_series(1, ?) g",
            [$source->jurisdiction_code, $source->source_id, $count],
        );

        $body = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?bbox=-110,49,-100,60&zoom=6&layers=mineral_disposition')
            ->assertOk()
            ->json();

        $meta = $body['polygon_layers']['mineral_disposition'];
        $this->assertSame($count, $meta['total_in_view']);
        $this->assertSame(PublicGeoscienceMapController::MAX_POLYGONS_PER_LAYER, $meta['returned']);
        $this->assertTrue($meta['truncated']);
        $this->assertCount(PublicGeoscienceMapController::MAX_POLYGONS_PER_LAYER, $body['polygons']['features']);
    }

    public function test_sync_status_counts_rows_per_layer_and_jurisdiction(): void
    {
        $this->seedParcel(-105.5, 57.5);

        $body = $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/sync-status')
            ->assertOk()
            ->json();

        $row = collect($body['layers'])->firstWhere('layer', 'mineral_disposition');
        $this->assertNotNull($row);
        $this->assertSame(1, $row['rows']);
        $this->assertNotNull($body['last_seen_at']);
    }
}
