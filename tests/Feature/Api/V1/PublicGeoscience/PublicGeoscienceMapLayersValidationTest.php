<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Http\Controllers\Api\V1\PublicGeoscience\PublicGeoscienceMapController;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * The `layers=` contract of GET /api/v1/public-geoscience/map that does not
 * need PostGIS: validation runs before any query, and the tolerance maths is
 * pure. The data behaviour is in PublicGeoscienceMapPolygonLayersTest
 * (Postgres suite).
 */
final class PublicGeoscienceMapLayersValidationTest extends TestCase
{
    use RefreshDatabase;

    public function test_unknown_polygon_layer_is_422(): void
    {
        $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?layers=mineral_disposition,pg_secret_table')
            ->assertUnprocessable()
            ->assertJsonValidationErrors('layers');
    }

    public function test_sql_shaped_layer_value_is_422(): void
    {
        $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/map?layers='.urlencode('mineral_disposition;DROP TABLE x'))
            ->assertUnprocessable();
    }

    public function test_the_four_polygon_tables_are_the_allowed_set(): void
    {
        $this->assertSame(
            ['mineral_disposition', 'resource_potential_zone', 'assessment_survey', 'bedrock_geology'],
            array_keys(PublicGeoscienceMapController::POLYGON_LAYERS),
        );
    }

    public function test_simplification_tolerance_is_about_half_a_pixel_and_shrinks_with_zoom(): void
    {
        $z4 = PublicGeoscienceMapController::simplifyTolerance(4);
        $z10 = PublicGeoscienceMapController::simplifyTolerance(10);

        $this->assertEqualsWithDelta(360 / (256 * 16) / 2, $z4, 1e-12);
        $this->assertGreaterThan($z10, $z4);
        $this->assertEqualsWithDelta($z4 / 64, $z10, 1e-12);
        $this->assertGreaterThan(0.0, PublicGeoscienceMapController::simplifyTolerance(22));
    }
}
