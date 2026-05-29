<?php

declare(strict_types=1);

namespace Database\Factories\Silver;

use App\Models\Silver\DecisionRecord;
use App\Models\User;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Str;

/**
 * DecisionRecord factory — doc-phase 110.
 *
 * Eight decision types per §21.3.
 *
 * @extends Factory<DecisionRecord>
 */
class DecisionRecordFactory extends Factory
{
    protected $model = DecisionRecord::class;

    public function definition(): array
    {
        return [
            'decision_id'         => (string) Str::uuid(),
            'workspace_id'        => (string) Str::uuid(),
            'decision_type'       => $this->faker->randomElement([
                'target_recommendation', 'crs_decision', 'schema_mapping',
                'public_data_import', 'export_approval', 'workflow_enablement',
                'conflict_resolution', 'report_signoff',
            ]),
            'recommendation'      => $this->faker->paragraph(),
            'human_decision'      => $this->faker->randomElement(['accepted', 'modified', 'rejected']),
            'reason'              => $this->faker->sentence(),
            'uncertainty'         => $this->faker->randomFloat(3, 0, 1),
            'decided_by_user_id'  => User::factory(),
            'decided_at'          => now(),
            'hash'                => null,
            'audit_ledger_id'     => null,
        ];
    }

    public function targetRecommendation(): static
    {
        return $this->state(fn () => ['decision_type' => 'target_recommendation']);
    }

    public function reportSignoff(): static
    {
        return $this->state(fn () => ['decision_type' => 'report_signoff']);
    }
}
