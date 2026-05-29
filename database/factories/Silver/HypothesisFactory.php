<?php

declare(strict_types=1);

namespace Database\Factories\Silver;

use App\Models\Silver\Hypothesis;
use App\Models\User;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Str;

/**
 * Hypothesis factory — doc-phase 110.
 *
 * @extends Factory<Hypothesis>
 */
class HypothesisFactory extends Factory
{
    protected $model = Hypothesis::class;

    public function definition(): array
    {
        return [
            'hypothesis_id'        => (string) Str::uuid(),
            'workspace_id'         => (string) Str::uuid(),
            'parent_question'      => $this->faker->sentence(10) . '?',
            'label'                => $this->faker->randomElement(['A', 'B', 'C', 'D']),
            'description'          => $this->faker->paragraph(),
            'confidence'           => $this->faker->randomFloat(3, 0.1, 0.95),
            'confidence_method'    => $this->faker->randomElement(['bayesian', 'heuristic']),
            'review_status'        => 'ai_suggested',
            'reviewed_by_user_id'  => null,
            'reviewed_at'          => null,
            'rationale'            => null,
            'created_at'           => now(),
        ];
    }

    /**
     * State: reviewed + accepted by a geologist.
     */
    public function accepted(): static
    {
        return $this->state(fn () => [
            'review_status' => 'accepted',
            'reviewed_by_user_id' => User::factory(),
            'reviewed_at' => now(),
            'rationale' => $this->faker->sentence(),
        ]);
    }

    /**
     * State: reviewed + rejected.
     */
    public function rejected(): static
    {
        return $this->state(fn () => [
            'review_status' => 'rejected',
            'reviewed_by_user_id' => User::factory(),
            'reviewed_at' => now(),
            'rationale' => $this->faker->sentence(),
        ]);
    }
}
