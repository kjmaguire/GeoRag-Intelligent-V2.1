<?php

declare(strict_types=1);

namespace Tests\Feature\Internal;

use App\Events\WorkspaceDataUpdated;
use App\Jobs\DebounceWorkspaceMvRefresh;
use App\Models\Project;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Facades\Redis;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Phase 2 of the reliability spec — broadcast controller side-effects.
 *
 * Locks the spec's "side effects fire only from confirmed terminal
 * completions" invariant:
 *
 *   T9  — data_version bump fires only on status='completed', never on
 *         queued/started/failed/cancelled/timed_out.
 *   T16 — workspace.data_updated is NOT emitted from this controller
 *         (it's emitted from the debounce job AFTER refresh confirms).
 */
final class IngestionProgressBroadcastControllerTest extends TestCase
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
            [$this->workspaceId, 'Broadcast Test', 'bct-'.substr($this->workspaceId, 0, 8)],
        );
        $this->project = Project::factory()->create(['data_version' => 0]);
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid, data_version = 0 WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );

        try {
            Redis::del("mv_refresh:last_dispatch:{$this->workspaceId}");
        } catch (\Throwable) {
            // Redis may be unreachable in some test envs — the bumper's
            // SETNX falls through to a no-op acquire = false branch.
        }
    }

    private function payload(string $status, array $overrides = []): array
    {
        return array_merge([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'pipeline_run_id' => (string) Str::uuid(),
            'stage' => 'parse',
            'status' => $status,
        ], $overrides);
    }

    public function test_completed_status_bumps_data_version_and_dispatches_refresh(): void
    {
        Event::fake([WorkspaceDataUpdated::class]);
        Queue::fake();

        $resp = $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/ingest-progress/broadcast', $this->payload('completed'));

        $resp->assertOk()
            ->assertJsonPath('side_effects.data_version_bumped', true)
            ->assertJsonPath('side_effects.mv_refresh_dispatched', true);

        $wsv = DB::scalar(
            'SELECT data_version FROM silver.workspaces WHERE workspace_id = ?::uuid',
            [$this->workspaceId],
        );
        $this->assertSame(1, (int) $wsv);

        // T16 — controller does NOT emit WorkspaceDataUpdated directly.
        // That's the debounce job's responsibility after refresh confirms.
        Event::assertNotDispatched(WorkspaceDataUpdated::class);

        Queue::assertPushed(DebounceWorkspaceMvRefresh::class);
    }

    public function test_failed_status_does_not_bump_or_dispatch(): void
    {
        Event::fake([WorkspaceDataUpdated::class]);
        Queue::fake();

        $resp = $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/ingest-progress/broadcast', $this->payload('failed'));

        $resp->assertOk()
            ->assertJsonPath('side_effects.data_version_bumped', false)
            ->assertJsonPath('side_effects.mv_refresh_dispatched', false);

        $wsv = DB::scalar(
            'SELECT data_version FROM silver.workspaces WHERE workspace_id = ?::uuid',
            [$this->workspaceId],
        );
        $this->assertSame(0, (int) $wsv, 'failed runs must not bump data_version');

        Event::assertNotDispatched(WorkspaceDataUpdated::class);
        Queue::assertNotPushed(DebounceWorkspaceMvRefresh::class);
    }

    public function test_timed_out_status_does_not_bump_or_dispatch(): void
    {
        Queue::fake();

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/ingest-progress/broadcast', $this->payload('timed_out'))
            ->assertOk()
            ->assertJsonPath('side_effects.data_version_bumped', false);

        $wsv = DB::scalar(
            'SELECT data_version FROM silver.workspaces WHERE workspace_id = ?::uuid',
            [$this->workspaceId],
        );
        $this->assertSame(0, (int) $wsv);
        Queue::assertNotPushed(DebounceWorkspaceMvRefresh::class);
    }

    public function test_completed_dispatch_is_idempotent_for_same_run_id(): void
    {
        Queue::fake();

        // Hatchet retries of the same terminal-state broadcast must not
        // double-bump data_version. Two POSTs with the same
        // pipeline_run_id should leave data_version at 1, not 2.
        $payload = $this->payload('completed');

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/ingest-progress/broadcast', $payload)
            ->assertOk();

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/ingest-progress/broadcast', $payload)
            ->assertOk()
            ->assertJsonPath('side_effects.data_version_bumped', false)
            ->assertJsonPath('side_effects.mv_refresh_dispatched', true);

        $wsv = DB::scalar(
            'SELECT data_version FROM silver.workspaces WHERE workspace_id = ?::uuid',
            [$this->workspaceId],
        );
        $this->assertSame(1, (int) $wsv, 'Idempotent guard must prevent double-bump');
    }

    public function test_missing_service_key_is_rejected(): void
    {
        $this->postJson('/api/internal/v1/ingest-progress/broadcast', $this->payload('completed'))
            ->assertUnauthorized();
    }
}
