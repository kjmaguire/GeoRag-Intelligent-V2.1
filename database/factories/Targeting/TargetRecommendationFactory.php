<?php

declare(strict_types=1);

namespace Database\Factories\Targeting;

use App\Models\Project;
use App\Models\Targeting\TargetRecommendation;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Str;

/**
 * TargetRecommendation factory — doc-phase 110.
 *
 * @extends Factory<TargetRecommendation>
 */
class TargetRecommendationFactory extends Factory
{
    protected $model = TargetRecommendation::class;

    public function definition(): array
    {
        return [
            'recommendation_id'    => (string) Str::uuid(),
            'workspace_id'         => (string) Str::uuid(),
            'project_id'           => Project::factory(),
            'run_id'               => (string) Str::uuid(),
            'zone_id'              => (string) Str::uuid(),
            'score_id'             => (string) Str::uuid(),
            'rank'                 => $this->faker->numberBetween(1, 25),
            'explanation_markdown' => "Highest-ranked untested target zone based on "
                                      . "current evidence. " . $this->faker->paragraph(),
            'created_at'           => now(),
        ];
    }

    /**
     * State: top-ranked recommendation (rank=1).
     */
    public function topRanked(): static
    {
        return $this->state(fn () => ['rank' => 1]);
    }
}
