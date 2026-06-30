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
 * Phase 0 Step 5.2 — feature coverage for /admin/agent-config/workspaces.
 *
 * Asserts:
 *   - guest → /login
 *   - non-admin → 403
 *   - admin sees the seeded row in props
 *   - admin PATCH mutates enabled + config JSONB and writes a
 *     workspace.workspace_agent_config.update audit_ledger row carrying
 *     the workspace_id (so RLS-scoped audit reads can find it).
 */
class WorkspacesTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private const AGENT = 'Storage Tiering Agent';

    /** @return array{config_id: string, workspace_id: string} */
    private function seedFixture(): array
    {
        $workspaceId = (string) Str::uuid();
        DB::connection('pgsql')->table('silver.workspaces')->insert([
            'workspace_id' => $workspaceId,
            'name' => 'Phase 0 admin test workspace',
            'slug' => 'phase0-admin-test-'.substr($workspaceId, 0, 8),
        ]);

        $configId = (string) Str::uuid();
        DB::connection('pgsql')->table('workspace.workspace_agent_config')->insert([
            'id' => $configId,
            'workspace_id' => $workspaceId,
            'agent_name' => self::AGENT,
            'config' => '{"hot_threshold_days": 7}',
            'enabled' => true,
        ]);

        return ['config_id' => $configId, 'workspace_id' => $workspaceId];
    }

    public function test_guest_is_redirected_to_login(): void
    {
        $this->get('/admin/agent-config/workspaces')->assertRedirect('/login');
    }

    public function test_non_admin_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');
        $this->get('/admin/agent-config/workspaces')->assertForbidden();
    }

    public function test_admin_sees_seeded_row(): void
    {
        $ids = $this->seedFixture();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->get('/admin/agent-config/workspaces');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/AgentConfig/Workspaces')
            ->has('workspace_agent_configs')
            ->where(
                'workspace_agent_configs',
                fn ($rows) => collect($rows)->contains(fn ($r) => $r['id'] === $ids['config_id']),
            ),
        );
    }

    public function test_admin_update_mutates_row_and_audit_ledger(): void
    {
        $ids = $this->seedFixture();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->patch("/admin/agent-config/workspaces/{$ids['config_id']}", [
            'enabled' => false,
            'config' => ['hot_threshold_days' => 14, 'warm_to_cold_days' => 60],
        ]);
        $response->assertRedirect(route('admin.agent-config.workspaces'));

        $row = DB::connection('pgsql')->table('workspace.workspace_agent_config')
            ->where('id', $ids['config_id'])->first();
        $this->assertFalse((bool) $row->enabled);
        $config = is_string($row->config) ? json_decode($row->config, true) : (array) $row->config;
        $this->assertSame(14, $config['hot_threshold_days']);
        $this->assertSame(60, $config['warm_to_cold_days']);
        $this->assertSame((int) $admin->id, (int) $row->updated_by);

        $audit = DB::connection('pgsql')->table('audit.audit_ledger')
            ->where('action_type', 'workspace.workspace_agent_config.update')
            ->where('target_id', $ids['config_id'])
            ->orderByDesc('created_at')
            ->first();
        $this->assertNotNull($audit);
        $this->assertSame((int) $admin->id, (int) $audit->actor_id);
        $this->assertSame($ids['workspace_id'], $audit->workspace_id);
        $this->assertSame('workspace', $audit->target_schema);
        $this->assertSame('workspace_agent_config', $audit->target_table);
    }

    public function test_update_rejects_non_object_config(): void
    {
        $ids = $this->seedFixture();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->patch("/admin/agent-config/workspaces/{$ids['config_id']}", [
            'enabled' => true,
            'config' => 'not-an-object',
        ]);
        $response->assertSessionHasErrors('config');
    }
}
