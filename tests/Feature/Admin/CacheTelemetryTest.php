<?php

declare(strict_types=1);

namespace Tests\Feature\Admin;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Phase 37 R-P21-CACHE-TELEMETRY-DASHBOARD — feature coverage for
 * the cache-telemetry JSON endpoint.
 *
 * Assertions follow the same shape as WorkflowRunDashboardTest:
 *   - guest → 302 redirect to /login
 *   - non-admin authenticated user → 403
 *   - admin → 200 + JSON body with the documented shape
 *   - inserted answer_runs rows are reflected in the totals + breakdown
 *   - window_hours query param clamps to [1, 168]
 *
 * Gated on PG via RequiresPostgres — the endpoint reads from
 * silver.answer_runs and the cache_skipped_reason column added in
 * Phase 30's DDL migration.
 */
class CacheTelemetryTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private const ENDPOINT = '/admin/cache-telemetry/skip-reasons.json';

    public function test_guest_is_redirected_to_login(): void
    {
        $response = $this->get(self::ENDPOINT);
        $response->assertRedirect('/login');
    }

    public function test_non_admin_user_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');

        $response = $this->get(self::ENDPOINT);
        $response->assertForbidden();
    }

    public function test_admin_sees_documented_json_shape(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $response = $this->get(self::ENDPOINT);
        $response->assertOk();
        $response->assertJsonStructure([
            'window_hours',
            'totals' => ['hits', 'misses', 'total', 'hit_rate'],
            'skipped_reasons' => [
                'zero_candidates',
                'partial_failures',
                'schema_validation_failed',
                'downhole_bypass_legacy',
                '(none)',
            ],
            'last_hour' => ['hits', 'misses', 'total', 'hit_rate'],
        ]);
    }

    public function test_inserted_rows_appear_in_totals(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        // Insert 3 rows: 1 cache hit, 2 misses (one with zero_candidates skip).
        $ws = '00000000-0000-0000-0000-00000000aaaa';
        $hitOfRunId = '00000000-0000-0000-0000-000000000001';

        DB::connection('pgsql')->table('silver.answer_runs')->insert([
            [
                'answer_run_id' => $hitOfRunId,
                'workspace_id' => $ws,
                'query_text' => 'parent run for hit test',
                'query_class' => 'count',
                'workspace_data_version_at_query' => 1,
                'cache_hit_of_run_id' => null,
                'cache_skipped_reason' => null,
                'created_at' => now()->subMinutes(2),
                'updated_at' => now()->subMinutes(2),
            ],
            [
                'answer_run_id' => '00000000-0000-0000-0000-000000000002',
                'workspace_id' => $ws,
                'query_text' => 'cache hit run',
                'query_class' => 'count',
                'workspace_data_version_at_query' => 1,
                'cache_hit_of_run_id' => $hitOfRunId,
                'cache_skipped_reason' => null,
                'created_at' => now()->subMinutes(1),
                'updated_at' => now()->subMinutes(1),
            ],
            [
                'answer_run_id' => '00000000-0000-0000-0000-000000000003',
                'workspace_id' => $ws,
                'query_text' => 'cache miss zero candidates',
                'query_class' => 'count',
                'workspace_data_version_at_query' => 1,
                'cache_hit_of_run_id' => null,
                'cache_skipped_reason' => 'zero_candidates',
                'created_at' => now()->subSeconds(30),
                'updated_at' => now()->subSeconds(30),
            ],
        ]);

        $response = $this->get(self::ENDPOINT);
        $response->assertOk();
        $body = $response->json();

        // At least 1 hit + 2 misses present in last 24h totals.
        $this->assertGreaterThanOrEqual(1, $body['totals']['hits']);
        $this->assertGreaterThanOrEqual(2, $body['totals']['misses']);
        // zero_candidates skip reason ≥ 1.
        $this->assertGreaterThanOrEqual(1, $body['skipped_reasons']['zero_candidates']);
    }

    public function test_window_hours_query_param_clamps(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        // out-of-range values fall back to the 24h default
        $this->get(self::ENDPOINT.'?window_hours=0')->assertJson(['window_hours' => 24]);
        $this->get(self::ENDPOINT.'?window_hours=999')->assertJson(['window_hours' => 24]);
        // in-range values are honoured
        $this->get(self::ENDPOINT.'?window_hours=6')->assertJson(['window_hours' => 6]);
    }
}
