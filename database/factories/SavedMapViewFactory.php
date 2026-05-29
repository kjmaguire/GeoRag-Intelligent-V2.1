<?php

declare(strict_types=1);

namespace Database\Factories;

use App\Models\Project;
use App\Models\SavedMapView;
use App\Models\User;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Str;

/**
 * SavedMapView factory — doc-phase 107.
 *
 * Seeds feature tests that need to exercise the §6.5 saved-map-views
 * endpoints. view_state defaults to a small but realistic MapLibre
 * camera + active-layer payload so JSON serialization round-trips
 * are exercised in tests.
 *
 * @extends Factory<SavedMapView>
 */
class SavedMapViewFactory extends Factory
{
    protected $model = SavedMapView::class;

    public function definition(): array
    {
        return [
            'view_id'      => (string) Str::uuid(),
            'workspace_id' => (string) Str::uuid(),
            'project_id'   => Project::factory(),
            'user_id'      => User::factory(),
            'name'         => $this->faker->unique()->words(3, true),
            'description'  => $this->faker->optional()->sentence(),
            'view_state'   => [
                'camera' => [
                    'longitude' => $this->faker->randomFloat(4, -110, -100),
                    'latitude'  => $this->faker->randomFloat(4, 50, 60),
                    'zoom'      => $this->faker->randomFloat(1, 5, 14),
                    'bearing'   => 0,
                    'pitch'     => 0,
                ],
                'active_layer_pack' => $this->faker->randomElement([
                    'private_project', 'public_geo', 'qa', 'target',
                ]),
                'filters' => [],
            ],
            'aoi_geom'  => null,
            'is_shared' => false,
        ];
    }

    /**
     * State: shared with the workspace (is_shared=true).
     */
    public function shared(): static
    {
        return $this->state(fn () => ['is_shared' => true]);
    }
}
