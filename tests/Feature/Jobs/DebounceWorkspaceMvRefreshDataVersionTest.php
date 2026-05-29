<?php

declare(strict_types=1);

namespace Tests\Feature\Jobs;

use App\Events\WorkspaceDataUpdated;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Phase 4 + Phase 5 — DebounceWorkspaceMvRefresh threads
 * silver.projects.data_version through to WorkspaceDataUpdated so
 * MapView's MVT cache-bust suffix advances on every successful refresh.
 *
 * The job re-queries data_version at dispatch time (not job-construction
 * time) so multiple completions coalescing into one debounced run all
 * surface the same final version.
 */
final class DebounceWorkspaceMvRefreshDataVersionTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    public function test_workspace_data_updated_event_has_data_version_field(): void
    {
        // This test verifies the event class accepts and forwards the new
        // dataVersion field. The full job execution requires Redis +
        // FastAPI MV-refresh endpoint mocking that lives in the Phase 1
        // broadcast controller test; here we lock the event shape itself.
        Event::fake([WorkspaceDataUpdated::class]);

        $workspaceId = (string) Str::uuid();
        $projectId = (string) Str::uuid();
        $pipelineRunId = (string) Str::uuid();

        WorkspaceDataUpdated::dispatch(
            $workspaceId,
            $projectId,
            $pipelineRunId,
            ['reports', 'collars'],
            42,  // Phase 4 — data_version field
        );

        Event::assertDispatched(
            WorkspaceDataUpdated::class,
            function (WorkspaceDataUpdated $e): bool {
                return $e->dataVersion === 42
                    && in_array('reports', $e->affectedTypes, true);
            },
        );
    }

    public function test_workspace_data_updated_broadcasts_data_version_in_payload(): void
    {
        $workspaceId = (string) Str::uuid();
        $projectId = (string) Str::uuid();
        $pipelineRunId = (string) Str::uuid();

        $event = new WorkspaceDataUpdated(
            $workspaceId,
            $projectId,
            $pipelineRunId,
            ['collars'],
            7,
        );

        $payload = $event->broadcastWith();

        $this->assertSame(7, $payload['data_version']);
        $this->assertSame($projectId, $payload['project_id']);
        $this->assertSame(['collars'], $payload['affected_types']);
    }

    public function test_data_version_null_when_omitted(): void
    {
        // Phase 1 callers pre-date the data_version field. The default is
        // null and broadcastWith() must surface it — the React hook treats
        // null as "no new version info, don't touch tiles".
        $event = new WorkspaceDataUpdated(
            (string) Str::uuid(),
            (string) Str::uuid(),
            (string) Str::uuid(),
            ['audit_log'],
        );

        $payload = $event->broadcastWith();

        $this->assertNull($payload['data_version']);
    }
}
