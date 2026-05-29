<?php

declare(strict_types=1);

namespace Tests\Feature\Internal;

use App\Events\WorkspaceDataUpdated;
use App\Models\Project;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Reliability spec Phase 2b — non-ingestion workspace updates bridge.
 *
 * Locks the invariant that the new bridge dispatches WorkspaceDataUpdated
 * directly (no data_version bump, no MV-refresh job) because its callers
 * are workflows whose tables are queryable the moment they commit
 * (e.g. score_targets writing targeting.target_recommendations).
 */
final class WorkspaceDataUpdatedBridgeControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private string $serviceKey;

    private string $workspaceId;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        $this->serviceKey = (string) (env('FASTAPI_SERVICE_KEY')
            ?: 'georag-service-key-dev-test-32bytes-or-more-for-validator-ok');
        config(['services.fastapi.service_key' => $this->serviceKey]);
        putenv("FASTAPI_SERVICE_KEY={$this->serviceKey}");

        $this->workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, data_version, created_at, updated_at)
             VALUES (?::uuid, ?, ?, 0, NOW(), NOW())',
            [$this->workspaceId, 'WSU Bridge Test', 'wsubt-'.substr($this->workspaceId, 0, 8)],
        );
        $this->project = Project::factory()->create(['data_version' => 0]);
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
    }

    /**
     * @return array<string, mixed>
     */
    private function payload(array $overrides = []): array
    {
        return array_merge([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'pipeline_run_id' => (string) Str::uuid(),
            'affected_types' => ['targets'],
        ], $overrides);
    }

    public function test_valid_payload_dispatches_workspace_data_updated(): void
    {
        Event::fake([WorkspaceDataUpdated::class]);

        $payload = $this->payload();

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/workspace-data-updated', $payload)
            ->assertOk()
            ->assertJsonPath('ok', true);

        Event::assertDispatched(
            WorkspaceDataUpdated::class,
            function (WorkspaceDataUpdated $e) use ($payload): bool {
                return $e->workspaceId === $payload['workspace_id']
                    && $e->projectId === $payload['project_id']
                    && $e->pipelineRunId === $payload['pipeline_run_id']
                    && $e->affectedTypes === ['targets'];
            },
        );
    }

    public function test_data_version_is_not_bumped(): void
    {
        // Invariant: this bridge does not run any MV-refresh / data_version
        // side-effects. Those belong to the ingestion bridge.
        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/workspace-data-updated', $this->payload())
            ->assertOk();

        $wsv = DB::scalar(
            'SELECT data_version FROM silver.workspaces WHERE workspace_id = ?::uuid',
            [$this->workspaceId],
        );
        $this->assertSame(0, (int) $wsv, 'WSU bridge must not bump workspace data_version');
    }

    public function test_missing_service_key_is_rejected(): void
    {
        Event::fake([WorkspaceDataUpdated::class]);

        $this->postJson('/api/internal/v1/workspace-data-updated', $this->payload())
            ->assertUnauthorized();

        Event::assertNotDispatched(WorkspaceDataUpdated::class);
    }

    public function test_empty_affected_types_is_rejected(): void
    {
        Event::fake([WorkspaceDataUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/workspace-data-updated',
                $this->payload(['affected_types' => []]),
            )
            ->assertStatus(422);

        Event::assertNotDispatched(WorkspaceDataUpdated::class);
    }

    public function test_malformed_uuids_are_rejected(): void
    {
        Event::fake([WorkspaceDataUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/workspace-data-updated',
                $this->payload(['project_id' => 'not-a-uuid']),
            )
            ->assertStatus(422);

        Event::assertNotDispatched(WorkspaceDataUpdated::class);
    }

    public function test_multiple_affected_types_pass_through(): void
    {
        Event::fake([WorkspaceDataUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/workspace-data-updated',
                $this->payload(['affected_types' => ['targets', 'reports', 'quality']]),
            )
            ->assertOk();

        Event::assertDispatched(
            WorkspaceDataUpdated::class,
            fn (WorkspaceDataUpdated $e) => $e->affectedTypes === ['targets', 'reports', 'quality'],
        );
    }
}
