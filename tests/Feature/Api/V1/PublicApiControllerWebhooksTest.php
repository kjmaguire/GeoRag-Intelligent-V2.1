<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Regression coverage for PublicApiController::webhooks()
 * (GET /api/v1/webhooks).
 *
 * Fixed 2026-09-15. Two defects, both live until now:
 *
 *  1. The query named six columns — flow_type, target_url,
 *     last_attempted_at, last_status — that workflow.flow_registry has
 *     never had, and filtered on flow_type = 'outbound_webhook', a value
 *     flow_registry_kind_check would reject even if the column existed.
 *     Every call returned Postgres 42703 undefined_column, so the endpoint
 *     had never once succeeded.
 *  2. It had no authorization check beyond the route group's Sanctum auth.
 *     flow_registry is platform configuration exempted from tenant
 *     isolation by name in phase0/98-rls-tenant-isolation-block3.sql, so
 *     there is no workspace_id to scope on; the gate is is_admin, matching
 *     /admin/integrations, which reads the same table.
 *
 * Postgres-only: workflow.flow_registry is provisioned by a migration that
 * returns early on any non-pgsql driver, so under the default SQLite suite
 * the table does not exist. Registered in phpunit.pgsql.xml.
 */
class PublicApiControllerWebhooksTest extends TestCase
{
    use RefreshDatabase;

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('workflow.flow_registry exists only on Postgres.');
        }
    }

    public function test_unauthenticated_webhooks_returns_401(): void
    {
        $this->getJson('/api/v1/webhooks')->assertUnauthorized();
    }

    public function test_non_admin_user_is_refused(): void
    {
        $user = User::factory()->create(['is_admin' => false]);

        $this->actingAs($user, 'sanctum')
            ->getJson('/api/v1/webhooks')
            ->assertNotFound()
            ->assertJson(['error' => 'not_found']);
    }

    public function test_admin_sees_only_webhook_kind_flows(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);

        $response = $this->actingAs($admin, 'sanctum')
            ->getJson('/api/v1/webhooks')
            ->assertOk();

        $names = collect($response->json('items'))->pluck('flow_name')->all();

        // Seeded by the flow_registry provisioning migration.
        $this->assertContains('external_notification', $names,
            'The one inbound-webhook flow must be listed.');
        $this->assertNotContains('public_geoscience_pull', $names,
            'A scheduled-import flow is not a webhook.');
        $this->assertNotContains('phase2_smoke', $names,
            'A placeholder flow is not a webhook.');
        $this->assertSame(count($names), $response->json('count'));
    }

    public function test_response_never_carries_the_per_flow_jwt_secret(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);

        $items = $this->actingAs($admin, 'sanctum')
            ->getJson('/api/v1/webhooks')
            ->assertOk()
            ->json('items');

        $this->assertNotEmpty($items, 'Need at least one row to assert against.');

        foreach ($items as $item) {
            $this->assertArrayNotHasKey('jwt_secret_kid', $item);
            $this->assertArrayNotHasKey('jwt_secret_ciphertext', $item);
        }
    }
}
