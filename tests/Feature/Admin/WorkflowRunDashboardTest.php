<?php

declare(strict_types=1);

namespace Tests\Feature\Admin;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Phase 0 Step 3 — basic feature coverage for the Workflow Run Dashboard.
 *
 * Phase 0 done-definition only requires "a basic feature test", not the
 * golden-query gate that lands later. We assert:
 *   - unauthenticated → redirected to /login
 *   - authenticated non-admin → 403
 *   - admin user → 200, Inertia page renders, props include workflow_runs
 *     and tempo_url, inserted row shows up in the props
 *   - server-side filters narrow the result set
 *
 * The dashboard reads from workflow.workflow_runs (a partman-monthly-
 * partitioned PostgreSQL table) so this whole test class is gated on the
 * PG test connection via RequiresPostgres — it skips on the SQLite suite.
 */
class WorkflowRunDashboardTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    public function test_guest_is_redirected_to_login(): void
    {
        $response = $this->get('/admin/workflow-runs');
        $response->assertRedirect('/login');
    }

    public function test_non_admin_user_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');

        $response = $this->get('/admin/workflow-runs');
        $response->assertForbidden();
    }

    public function test_admin_sees_dashboard_with_inserted_row(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $traceId = bin2hex(random_bytes(16));
        DB::connection('pgsql')->table('workflow.workflow_runs')->insert([
            'workspace_id' => '00000000-0000-0000-0000-00000000abcd',
            'workflow_kind' => 'phase0_smoke',
            'engine' => 'hatchet',
            'status' => 'success',
            'trace_id' => $traceId,
            'started_at' => now()->subMinutes(5),
            'ended_at' => now(),
        ]);

        $response = $this->get('/admin/workflow-runs');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/WorkflowRuns')
            ->has('workflow_runs')
            ->where('tempo_url', config('services.tempo.url'))
            ->where('workflow_runs.0.workflow_kind', 'phase0_smoke')
            ->where('workflow_runs.0.trace_id', $traceId)
            ->where('workflow_runs.0.status', 'success')
        );
    }

    public function test_status_filter_narrows_results(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $base = ['workspace_id' => '00000000-0000-0000-0000-00000000abcd'];
        DB::connection('pgsql')->table('workflow.workflow_runs')->insert([
            $base + [
                'workflow_kind' => 'phase0_smoke',
                'engine' => 'hatchet',
                'status' => 'success',
                'trace_id' => bin2hex(random_bytes(16)),
                'started_at' => now()->subMinutes(10),
                'ended_at' => now()->subMinutes(9),
            ],
            $base + [
                'workflow_kind' => 'phase0_smoke',
                'engine' => 'hatchet',
                'status' => 'failure',
                'trace_id' => bin2hex(random_bytes(16)),
                'started_at' => now()->subMinutes(8),
                'ended_at' => now()->subMinutes(7),
            ],
        ]);

        $response = $this->get('/admin/workflow-runs?status=failure');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/WorkflowRuns')
            ->where('filters.status', 'failure')
            ->has('workflow_runs', 1)
            ->where('workflow_runs.0.status', 'failure')
        );
    }
}
