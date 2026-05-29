<?php

declare(strict_types=1);

namespace Tests\Feature\Internal;

use App\Events\Workspace\WorkspaceActivityBroadcast;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Phase 3 — workspace-level activity bridge.
 *
 * Locks the invariants:
 *   - Service-key auth required
 *   - workspace_id must be UUID
 *   - affected_types is required + non-empty
 *   - payload is optional (defaults to [])
 *   - Bridge dispatches WorkspaceActivityBroadcast with the correct shape
 */
final class WorkspaceActivityBridgeControllerTest extends TestCase
{
    use RefreshDatabase;

    private string $serviceKey;

    protected function setUp(): void
    {
        parent::setUp();

        $this->serviceKey = (string) (env('FASTAPI_SERVICE_KEY')
            ?: 'georag-service-key-dev-test-32bytes-or-more-for-validator-ok');
        config(['services.fastapi.service_key' => $this->serviceKey]);
        putenv("FASTAPI_SERVICE_KEY={$this->serviceKey}");
    }

    /**
     * @return array<string, mixed>
     */
    private function payload(array $overrides = []): array
    {
        return array_merge([
            'workspace_id' => (string) Str::uuid(),
            'affected_types' => ['projects', 'kpis'],
        ], $overrides);
    }

    public function test_valid_payload_dispatches_event(): void
    {
        Event::fake([WorkspaceActivityBroadcast::class]);

        $payload = $this->payload([
            'payload' => ['verb' => 'created', 'project_id' => 'abc-123'],
        ]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/workspace-activity', $payload)
            ->assertOk()
            ->assertJsonPath('ok', true);

        Event::assertDispatched(
            WorkspaceActivityBroadcast::class,
            function (WorkspaceActivityBroadcast $e) use ($payload): bool {
                return $e->workspaceId === $payload['workspace_id']
                    && $e->affectedTypes === ['projects', 'kpis']
                    && ($e->payload['verb'] ?? null) === 'created';
            },
        );
    }

    public function test_missing_service_key_is_rejected(): void
    {
        Event::fake([WorkspaceActivityBroadcast::class]);

        $this->postJson('/api/internal/v1/workspace-activity', $this->payload())
            ->assertUnauthorized();

        Event::assertNotDispatched(WorkspaceActivityBroadcast::class);
    }

    public function test_malformed_workspace_id_rejected(): void
    {
        Event::fake([WorkspaceActivityBroadcast::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/workspace-activity',
                $this->payload(['workspace_id' => 'not-a-uuid']),
            )
            ->assertStatus(422);

        Event::assertNotDispatched(WorkspaceActivityBroadcast::class);
    }

    public function test_empty_affected_types_rejected(): void
    {
        Event::fake([WorkspaceActivityBroadcast::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/workspace-activity',
                $this->payload(['affected_types' => []]),
            )
            ->assertStatus(422);

        Event::assertNotDispatched(WorkspaceActivityBroadcast::class);
    }

    public function test_payload_field_is_optional(): void
    {
        Event::fake([WorkspaceActivityBroadcast::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/workspace-activity',
                ['workspace_id' => (string) Str::uuid(), 'affected_types' => ['cost']],
            )
            ->assertOk();

        Event::assertDispatched(
            WorkspaceActivityBroadcast::class,
            fn (WorkspaceActivityBroadcast $e) => $e->payload === [],
        );
    }
}
