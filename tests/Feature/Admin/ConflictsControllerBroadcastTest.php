<?php

declare(strict_types=1);

namespace Tests\Feature\Admin;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Phase 5 — ConflictsController::run dispatches AdminSurfaceUpdated('conflicts')
 * on every successful FastAPI POST. Locks the wire contract.
 */
final class ConflictsControllerBroadcastTest extends TestCase
{
    use RefreshDatabase;

    public function test_successful_run_dispatches_conflicts_surface_event(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);
        Http::fake([
            '*/api/v1/admin/conflicts/run' => Http::response(['ok' => true, 'matches' => 0], 200),
        ]);

        $user = User::factory()->create(['is_admin' => true]);
        $workspaceId = (string) Str::uuid();

        $this->actingAs($user)
            ->postJson('/admin/conflicts/run', [
                'workspace_id' => $workspaceId,
                'section_id' => 'test-section',
                'claims' => [
                    ['claim_id' => 'c1', 'text' => 'test claim'],
                ],
            ])
            ->assertOk();

        Event::assertDispatched(
            AdminSurfaceUpdated::class,
            function (AdminSurfaceUpdated $e) use ($workspaceId): bool {
                return $e->surface === 'conflicts'
                    && $e->surfaceId === null
                    && $e->affectedProps === ['entries']
                    && ($e->payload['workspace_id'] ?? null) === $workspaceId
                    && ($e->payload['claim_count'] ?? null) === 1;
            },
        );
    }

    public function test_fastapi_failure_does_not_dispatch_event(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);
        Http::fake([
            '*/api/v1/admin/conflicts/run' => Http::response(['error' => 'upstream'], 500),
        ]);

        $user = User::factory()->create(['is_admin' => true]);

        $this->actingAs($user)
            ->postJson('/admin/conflicts/run', [
                'workspace_id' => (string) Str::uuid(),
                'claims' => [['claim_id' => 'c1', 'text' => 'test']],
            ])
            ->assertStatus(502);

        // No surface broadcast on upstream failure — the entries didn't change.
        Event::assertNotDispatched(AdminSurfaceUpdated::class);
    }
}
