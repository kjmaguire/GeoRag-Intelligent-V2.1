<?php

declare(strict_types=1);

namespace Tests\Feature\Admin\AgentConfig;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Phase 0 Step 5.2 — feature coverage for /admin/agent-config/timeouts.
 *
 * Asserts:
 *   - guest is redirected to /login
 *   - non-admin is forbidden
 *   - admin sees the page with the seeded row in props
 *   - admin PATCH to /timeouts/{agent_name} mutates the row + writes a
 *     workspace.agent_timeouts.update audit_ledger entry with the right
 *     actor_id and target_id, both committed in one transaction
 */
class TimeoutsTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private const AGENT = 'Tenant Isolation Auditor';

    private function seedAgentTimeout(): void
    {
        DB::connection('pgsql')->table('workspace.agent_timeouts')->updateOrInsert(
            ['agent_name' => self::AGENT],
            [
                'risk_tier' => 'R1',
                'soft_timeout_ms' => 30000,
                'hard_timeout_ms' => 120000,
                'retry_count' => 1,
                'circuit_breaker_scope' => 'workspace',
                'failure_threshold' => 5,
                'cool_down_seconds' => 300,
            ],
        );
    }

    public function test_guest_is_redirected_to_login(): void
    {
        $this->seedAgentTimeout();
        $this->get('/admin/agent-config/timeouts')->assertRedirect('/login');
    }

    public function test_non_admin_is_forbidden(): void
    {
        $this->seedAgentTimeout();
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');

        $this->get('/admin/agent-config/timeouts')->assertForbidden();
    }

    public function test_admin_sees_seeded_row(): void
    {
        $this->seedAgentTimeout();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->get('/admin/agent-config/timeouts');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/AgentConfig/Timeouts')
            ->has('timeouts')
            ->where(
                'timeouts',
                fn ($timeouts) => collect($timeouts)->contains(fn ($r) => $r['agent_name'] === self::AGENT),
            ),
        );
    }

    public function test_admin_update_mutates_row_and_audit_ledger(): void
    {
        $this->seedAgentTimeout();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $traceId = Str::random(32);
        $payload = [
            'soft_timeout_ms' => 45000,
            'hard_timeout_ms' => 180000,
            'retry_count' => 2,
            'circuit_breaker_scope' => 'global',
            'failure_threshold' => 7,
            'cool_down_seconds' => 600,
        ];

        $response = $this->withHeaders(['traceparent' => "00-{$traceId}-0000000000000001-01"])
            ->patch('/admin/agent-config/timeouts/'.urlencode(self::AGENT), $payload);

        $response->assertRedirect(route('admin.agent-config.timeouts'));

        $row = DB::connection('pgsql')->table('workspace.agent_timeouts')
            ->where('agent_name', self::AGENT)
            ->first();
        $this->assertNotNull($row);
        $this->assertSame(45000, (int) $row->soft_timeout_ms);
        $this->assertSame(180000, (int) $row->hard_timeout_ms);
        $this->assertSame(2, (int) $row->retry_count);
        $this->assertSame('global', $row->circuit_breaker_scope);
        $this->assertSame(7, (int) $row->failure_threshold);
        $this->assertSame(600, (int) $row->cool_down_seconds);
        $this->assertSame((int) $admin->id, (int) $row->updated_by);

        $audit = DB::connection('pgsql')->table('audit.audit_ledger')
            ->where('action_type', 'workspace.agent_timeouts.update')
            ->where('target_id', self::AGENT)
            ->orderByDesc('created_at')
            ->first();
        $this->assertNotNull($audit, 'expected audit_ledger row for update');
        $this->assertSame((int) $admin->id, (int) $audit->actor_id);
        $this->assertSame('user', $audit->actor_kind);
        $this->assertSame('workspace', $audit->target_schema);
        $this->assertSame('agent_timeouts', $audit->target_table);
    }

    public function test_update_validates_soft_lt_hard(): void
    {
        $this->seedAgentTimeout();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->patch('/admin/agent-config/timeouts/'.urlencode(self::AGENT), [
            'soft_timeout_ms' => 200000,
            'hard_timeout_ms' => 100000,
            'retry_count' => 1,
            'circuit_breaker_scope' => 'workspace',
            'failure_threshold' => 5,
            'cool_down_seconds' => 300,
        ]);

        $response->assertSessionHasErrors('soft_timeout_ms');
    }
}
