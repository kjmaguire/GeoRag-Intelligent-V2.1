<?php

declare(strict_types=1);

namespace Database\Factories\Ops;

use App\Models\Ops\SupportTicket;
use App\Models\User;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Str;

/**
 * SupportTicket factory — doc-phase 109.
 *
 * Seeds Customer Support Cockpit fixtures + replay tests.
 *
 * @extends Factory<SupportTicket>
 */
class SupportTicketFactory extends Factory
{
    protected $model = SupportTicket::class;

    public function definition(): array
    {
        return [
            'ticket_id' => (string) Str::uuid(),
            'workspace_id' => (string) Str::uuid(),
            'reported_by_user_id' => User::factory(),
            'reported_at' => now(),
            'channel' => $this->faker->randomElement(['in_app', 'email', 'webhook', 'phone']),
            'category' => $this->faker->randomElement([
                'wrong_answer', 'failed_ingestion', 'failed_report',
                'integration_issue', 'performance', 'other',
            ]),
            'description' => $this->faker->paragraph(),
            'severity' => $this->faker->randomElement(['low', 'medium', 'high', 'critical']),
            'assigned_to_user_id' => null,
            'status' => 'open',
            'resolution_summary' => null,
            'resolved_at' => null,
            'customer_visible_response' => null,
        ];
    }

    /**
     * State: assigned to an ops user.
     */
    public function assigned(): static
    {
        return $this->state(fn () => [
            'assigned_to_user_id' => User::factory(),
            'status' => 'investigating',
        ]);
    }

    /**
     * State: critical severity.
     */
    public function critical(): static
    {
        return $this->state(fn () => [
            'severity' => 'critical',
            'channel' => 'phone',
        ]);
    }

    /**
     * State: resolved.
     */
    public function resolved(): static
    {
        return $this->state(fn () => [
            'status' => 'resolved',
            'resolution_summary' => $this->faker->paragraph(),
            'resolved_at' => now(),
            'assigned_to_user_id' => User::factory(),
        ]);
    }
}
