<?php

declare(strict_types=1);

namespace Database\Factories\Eval;

use App\Models\Eval\GoldenQuestion;
use App\Models\User;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Str;

/**
 * GoldenQuestion factory — doc-phase 109.
 *
 * Seeds eval tests + Eval Dashboard fixtures.
 *
 * Default state: draft, mechanical schema_mapping question. Use the
 * `active()`, `retired()`, or per-set state helpers to vary.
 *
 * @extends Factory<GoldenQuestion>
 */
class GoldenQuestionFactory extends Factory
{
    protected $model = GoldenQuestion::class;

    public function definition(): array
    {
        return [
            'question_id'   => (string) Str::uuid(),
            'question_set'  => 'schema_mapping',
            'question_text' => $this->faker->sentence(8) . '?',
            'context_setup' => [],
            'expected_intent_class'        => null,
            'expected_citations'           => [],
            'expected_entities'            => [],
            'expected_numeric_values'      => [],
            'expected_refusal'             => false,
            'expected_refusal_reason'      => null,
            'expected_language_compliance' => [],
            'difficulty'                   => $this->faker->randomElement(['easy', 'medium', 'hard']),
            'authored_by_user_id'          => User::factory(),
            'authored_at'                  => now(),
            'reviewed_by_user_id'          => null,
            'reviewed_at'                  => null,
            'status'                       => 'draft',
        ];
    }

    /**
     * State: reviewed + active.
     */
    public function active(): static
    {
        return $this->state(fn () => [
            'status' => 'active',
            'reviewed_by_user_id' => User::factory(),
            'reviewed_at' => now(),
        ]);
    }

    /**
     * State: retired.
     */
    public function retired(): static
    {
        return $this->state(fn () => [
            'status' => 'retired',
            'reviewed_by_user_id' => User::factory(),
            'reviewed_at' => now()->subWeeks(2),
        ]);
    }

    /**
     * State: zero-tolerance question set (public_private_boundary).
     */
    public function publicPrivateBoundary(): static
    {
        return $this->state(fn () => [
            'question_set' => 'public_private_boundary',
            'expected_language_compliance' => [
                'must_contain' => ['public records', 'within 25 km'],
                'must_not_contain' => ['this project has uranium'],
            ],
        ]);
    }

    /**
     * State: target recommendation (R5; zero-tolerance regression).
     */
    public function targetRecommendation(): static
    {
        return $this->state(fn () => [
            'question_set' => 'target_recommendation',
            'expected_language_compliance' => [
                'must_not_contain' => ['drill here'],
                'must_contain' => ['highest-ranked', 'untested target'],
            ],
        ]);
    }

    /**
     * State: refusal expected (the system MUST refuse this question).
     */
    public function refusalExpected(string $reason = 'out_of_scope'): static
    {
        return $this->state(fn () => [
            'question_set' => 'refusal_correctness',
            'expected_refusal' => true,
            'expected_refusal_reason' => $reason,
        ]);
    }
}
