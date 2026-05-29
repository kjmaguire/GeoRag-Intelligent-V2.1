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
 * Phase 0 Step 5.2 — feature coverage for /admin/agent-config/pins.
 *
 * Verifies the pin/unpin flow:
 *   - admin sets prompt_version_id → row updated, audit row written
 *   - admin clears prompt_version_id (null) → row updated, second audit
 *     row written with new_prompt_version_id = null
 *   - cross-prompt mismatch is refused (422)
 */
class PinsTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private const AGENT = 'LLM Incident Diagnosis Agent';

    private const PROMPT_ID = 'llm_incident_diagnosis';

    private const OTHER_PROMPT_ID = 'support_packet';

    /** @return array{this_id: string, this_v2: string, other_id: string} */
    private function seedFixtures(): array
    {
        $thisId = (string) Str::uuid();
        $thisV2 = (string) Str::uuid();
        $otherId = (string) Str::uuid();

        DB::connection('pgsql')->table('workspace.prompt_versions')->insert([
            [
                'id' => $thisId,
                'prompt_id' => self::PROMPT_ID,
                'version' => 'v0.1.0',
                'text' => 'p1',
                'parameters' => '{}',
                'promotion_state' => 'production',
                'promoted_at' => now()->subDay(),
            ],
            [
                'id' => $thisV2,
                'prompt_id' => self::PROMPT_ID,
                'version' => 'v0.2.0',
                'text' => 'p2',
                'parameters' => '{}',
                'promotion_state' => 'staging',
            ],
            [
                'id' => $otherId,
                'prompt_id' => self::OTHER_PROMPT_ID,
                'version' => 'v0.1.0',
                'text' => 'other',
                'parameters' => '{}',
                'promotion_state' => 'production',
                'promoted_at' => now()->subDay(),
            ],
        ]);

        DB::connection('pgsql')->table('workspace.agent_prompt_pins')->updateOrInsert(
            ['agent_name' => self::AGENT],
            [
                'prompt_id' => self::PROMPT_ID,
                'prompt_version_id' => null,
                'pinned_at' => null,
                'pinned_by' => null,
            ],
        );

        return ['this_id' => $thisId, 'this_v2' => $thisV2, 'other_id' => $otherId];
    }

    public function test_guest_is_redirected_to_login(): void
    {
        $this->get('/admin/agent-config/pins')->assertRedirect('/login');
    }

    public function test_non_admin_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');
        $this->get('/admin/agent-config/pins')->assertForbidden();
    }

    public function test_admin_sees_pin_row_and_available_versions(): void
    {
        $this->seedFixtures();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->get('/admin/agent-config/pins');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/AgentConfig/Pins')
            ->has('pins')
            ->has('available_versions')
            ->where(
                'pins',
                fn ($pins) => collect($pins)->contains(fn ($r) => $r['agent_name'] === self::AGENT)
            )
        );
    }

    public function test_admin_pin_writes_row_and_audit(): void
    {
        $ids = $this->seedFixtures();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->patch(
            '/admin/agent-config/pins/'.urlencode(self::AGENT),
            ['prompt_version_id' => $ids['this_v2']],
        );
        $response->assertRedirect(route('admin.agent-config.pins'));

        $row = DB::connection('pgsql')->table('workspace.agent_prompt_pins')
            ->where('agent_name', self::AGENT)->first();
        $this->assertSame($ids['this_v2'], $row->prompt_version_id);
        $this->assertSame((int) $admin->id, (int) $row->pinned_by);
        $this->assertNotNull($row->pinned_at);

        $audit = DB::connection('pgsql')->table('audit.audit_ledger')
            ->where('action_type', 'workspace.agent_prompt_pins.update')
            ->where('target_id', self::AGENT)
            ->orderByDesc('created_at')
            ->first();
        $this->assertNotNull($audit);
        $this->assertSame((int) $admin->id, (int) $audit->actor_id);
        $this->assertSame('workspace', $audit->target_schema);
        $this->assertSame('agent_prompt_pins', $audit->target_table);
    }

    public function test_admin_unpin_clears_row(): void
    {
        $ids = $this->seedFixtures();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        // first pin
        $this->patch(
            '/admin/agent-config/pins/'.urlencode(self::AGENT),
            ['prompt_version_id' => $ids['this_v2']],
        )->assertRedirect();

        // then unpin
        $response = $this->patch(
            '/admin/agent-config/pins/'.urlencode(self::AGENT),
            ['prompt_version_id' => null],
        );
        $response->assertRedirect(route('admin.agent-config.pins'));

        $row = DB::connection('pgsql')->table('workspace.agent_prompt_pins')
            ->where('agent_name', self::AGENT)->first();
        $this->assertNull($row->prompt_version_id);
        $this->assertNull($row->pinned_at);
        $this->assertNull($row->pinned_by);
    }

    public function test_cross_prompt_mismatch_is_refused(): void
    {
        $ids = $this->seedFixtures();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->patch(
            '/admin/agent-config/pins/'.urlencode(self::AGENT),
            ['prompt_version_id' => $ids['other_id']],
        );
        $response->assertStatus(422);
    }
}
