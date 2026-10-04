<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * GET /api/v1/audit/{workspace} and /api/v1/usage/{workspace}.
 *
 * Any project member of the workspace used to be able to read the whole
 * workspace's audit ledger and cost rollups. They now need a workspace admin
 * (users.is_admin AND membership) or a project owner in that workspace. The
 * usage `days` window is bounded (it used to reach Postgres as an unbounded
 * `?::int`).
 *
 * Postgres-only: audit.audit_ledger and usage.* are raw-SQL tables.
 */
final class PublicApiControllerAdminGateTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private string $workspaceId;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$this->workspaceId, 'Admin Gate Workspace', 'ag-'.substr($this->workspaceId, 0, 8)],
        );
        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );

        DB::table('audit.audit_ledger')->insert([
            'workspace_id' => $this->workspaceId,
            'action_type' => 'test.admin_gate',
        ]);
        DB::table('usage.usage_aggregates_daily')->insert([
            'workspace_id' => $this->workspaceId,
            'agent_name' => 'chat',
            'model_profile' => 'default',
            'rollup_date' => now()->toDateString(),
            'invocations_total' => 3,
        ]);
    }

    private function member(string $role, bool $admin = false): User
    {
        $user = $admin ? User::factory()->admin()->create() : User::factory()->create();
        $user->projects()->syncWithoutDetaching([$this->project->project_id => ['role' => $role]]);

        return $user;
    }

    /**
     * @return array<string, array{0: string}>
     */
    public static function endpoints(): array
    {
        return ['audit' => ['audit'], 'usage' => ['usage']];
    }

    #[DataProvider('endpoints')]
    public function test_a_read_only_member_is_forbidden(string $endpoint): void
    {
        $this->actingAs($this->member('viewer'), 'sanctum')
            ->getJson("/api/v1/{$endpoint}/{$this->workspaceId}")
            ->assertForbidden();
    }

    #[DataProvider('endpoints')]
    public function test_a_non_member_still_gets_404(string $endpoint): void
    {
        $this->actingAs(User::factory()->create(), 'sanctum')
            ->getJson("/api/v1/{$endpoint}/{$this->workspaceId}")
            ->assertNotFound();
    }

    #[DataProvider('endpoints')]
    public function test_a_global_admin_who_is_not_in_the_workspace_gets_404(string $endpoint): void
    {
        $this->actingAs(User::factory()->admin()->create(), 'sanctum')
            ->getJson("/api/v1/{$endpoint}/{$this->workspaceId}")
            ->assertNotFound();
    }

    #[DataProvider('endpoints')]
    public function test_a_project_owner_is_allowed(string $endpoint): void
    {
        $this->actingAs($this->member('owner'), 'sanctum')
            ->getJson("/api/v1/{$endpoint}/{$this->workspaceId}")
            ->assertOk();
    }

    #[DataProvider('endpoints')]
    public function test_a_workspace_admin_member_is_allowed(string $endpoint): void
    {
        $this->actingAs($this->member('viewer', admin: true), 'sanctum')
            ->getJson("/api/v1/{$endpoint}/{$this->workspaceId}")
            ->assertOk();
    }

    public function test_the_owner_role_in_another_workspace_does_not_count(): void
    {
        $otherWorkspace = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$otherWorkspace, 'Other', 'ot-'.substr($otherWorkspace, 0, 8)],
        );
        $otherProject = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$otherWorkspace, $otherProject->project_id],
        );
        $user = $this->member('viewer');
        $user->projects()->syncWithoutDetaching([$otherProject->project_id => ['role' => 'owner']]);

        $this->actingAs($user, 'sanctum')
            ->getJson("/api/v1/audit/{$this->workspaceId}")
            ->assertForbidden();
    }

    public function test_a_36_character_non_uuid_workspace_is_404_not_a_500(): void
    {
        $this->actingAs($this->member('owner'), 'sanctum')
            ->getJson('/api/v1/usage/'.str_repeat('-', 36))
            ->assertNotFound();
    }

    /**
     * @return array<string, array{0: string, 1: int}>
     */
    public static function days(): array
    {
        return [
            'default' => ['', 30],
            'a valid window is honoured' => ['?days=7', 7],
            'a huge value is clamped to a year' => ['?days=99999999', 365],
            'past int4 is clamped, not a 500' => ['?days=99999999999999999999', 365],
            'negative falls back to the default' => ['?days=-5', 30],
            'zero falls back to the default' => ['?days=0', 30],
            'non-numeric falls back to the default' => ['?days=abc', 30],
        ];
    }

    #[DataProvider('days')]
    public function test_the_usage_window_is_clamped(string $query, int $expectedWindow): void
    {
        $this->actingAs($this->member('owner'), 'sanctum')
            ->getJson("/api/v1/usage/{$this->workspaceId}{$query}")
            ->assertOk()
            ->assertJsonPath('window_days', $expectedWindow)
            ->assertJsonPath('by_day.0.invocations', 3);
    }
}
