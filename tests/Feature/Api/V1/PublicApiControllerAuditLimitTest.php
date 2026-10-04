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
 * LAR-16 (2026-09-29 audit): GET /api/v1/audit/{workspace_id}?limit= had a
 * ceiling (`min($limit, 500)`) and no floor, so `?limit=-1` reached Postgres
 * as `LIMIT -1` — "LIMIT must not be negative" — and returned 500.
 *
 * Postgres-only: audit.audit_ledger is a raw-SQL table and the bound
 * `LIMIT ?` is only rejected by Postgres. Registered in phpunit.pgsql.xml.
 */
final class PublicApiControllerAuditLimitTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private string $workspaceId;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$this->workspaceId, 'Audit Limit Workspace', 'al-'.substr($this->workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $project->project_id],
        );

        $this->user = User::factory()->create();
        $this->user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'owner']]);

        for ($i = 0; $i < 3; $i++) {
            DB::table('audit.audit_ledger')->insert([
                'workspace_id' => $this->workspaceId,
                'action_type' => 'test.audit_limit',
            ]);
        }
    }

    /**
     * @return array<string, array{0: string, 1: int}>
     */
    public static function limits(): array
    {
        return [
            'negative falls back to the default' => ['-1', 3],
            'zero falls back to the default' => ['0', 3],
            'non-numeric falls back to the default' => ['abc', 3],
            'a small positive limit is honoured' => ['2', 2],
            'a huge limit is capped, not rejected' => ['1000000', 3],
        ];
    }

    #[DataProvider('limits')]
    public function test_limit_is_clamped(string $limit, int $expectedCount): void
    {
        $this->actingAs($this->user, 'sanctum')
            ->getJson('/api/v1/audit/'.$this->workspaceId.'?limit='.$limit)
            ->assertOk()
            ->assertJsonPath('count', $expectedCount);
    }
}
