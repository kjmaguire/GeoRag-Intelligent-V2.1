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
 * Phase 0 Step 5.2 — feature coverage for /admin/agent-config/prompts.
 *
 * Asserts that promoting a draft → production also demotes the previous
 * production version for that prompt_id (preserving the unique partial
 * index `prompt_versions_one_production_per_prompt`) AND writes a single
 * `workspace.prompt_versions.promote` audit_ledger row carrying the
 * promoting admin as actor_id.
 */
class PromptsTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private const PROMPT_ID = 'phase0_admin_test_prompt';

    /** @return array{old: string, new: string} */
    private function seedTwoVersions(): array
    {
        $old = (string) Str::uuid();
        $new = (string) Str::uuid();

        DB::connection('pgsql')->table('workspace.prompt_versions')->insert([
            [
                'id' => $old,
                'prompt_id' => self::PROMPT_ID,
                'version' => 'v0.1.0',
                'text' => 'old prompt body',
                'parameters' => '{}',
                'promotion_state' => 'production',
                'promoted_at' => now()->subDay(),
            ],
            [
                'id' => $new,
                'prompt_id' => self::PROMPT_ID,
                'version' => 'v0.2.0',
                'text' => 'new prompt body',
                'parameters' => '{}',
                'promotion_state' => 'staging',
            ],
        ]);

        return ['old' => $old, 'new' => $new];
    }

    public function test_guest_is_redirected_to_login(): void
    {
        $this->get('/admin/agent-config/prompts')->assertRedirect('/login');
    }

    public function test_non_admin_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');
        $this->get('/admin/agent-config/prompts')->assertForbidden();
    }

    public function test_admin_sees_grouped_versions(): void
    {
        $ids = $this->seedTwoVersions();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->get('/admin/agent-config/prompts');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/AgentConfig/Prompts')
            ->has('prompts')
            ->where(
                'prompts',
                fn ($prompts) => collect($prompts)->contains(
                    fn ($g) => $g['prompt_id'] === self::PROMPT_ID
                        && count($g['versions']) === 2
                )
            )
        );
        $this->assertNotEmpty($ids);
    }

    public function test_promotion_demotes_existing_production_and_writes_audit(): void
    {
        $ids = $this->seedTwoVersions();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->patch(
            "/admin/agent-config/prompts/{$ids['new']}/promote",
            ['promotion_state' => 'production'],
        );
        $response->assertRedirect(route('admin.agent-config.prompts'));

        $newRow = DB::connection('pgsql')->table('workspace.prompt_versions')
            ->where('id', $ids['new'])->first();
        $this->assertSame('production', $newRow->promotion_state);
        $this->assertNotNull($newRow->promoted_at);

        $oldRow = DB::connection('pgsql')->table('workspace.prompt_versions')
            ->where('id', $ids['old'])->first();
        $this->assertSame(
            'deprecated',
            $oldRow->promotion_state,
            'old production should auto-deprecate to keep the partial unique index satisfied'
        );

        $audit = DB::connection('pgsql')->table('audit.audit_ledger')
            ->where('action_type', 'workspace.prompt_versions.promote')
            ->where('target_id', $ids['new'])
            ->orderByDesc('created_at')
            ->first();
        $this->assertNotNull($audit);
        $this->assertSame((int) $admin->id, (int) $audit->actor_id);
        $this->assertSame('workspace', $audit->target_schema);
        $this->assertSame('prompt_versions', $audit->target_table);
    }

    public function test_invalid_promotion_state_is_rejected(): void
    {
        $ids = $this->seedTwoVersions();
        $admin = User::factory()->admin()->create();
        $this->actingAs($admin, 'sanctum');

        $response = $this->patch(
            "/admin/agent-config/prompts/{$ids['new']}/promote",
            ['promotion_state' => 'banana'],
        );
        $response->assertSessionHasErrors('promotion_state');
    }
}
