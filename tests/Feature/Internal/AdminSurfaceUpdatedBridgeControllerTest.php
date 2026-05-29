<?php

declare(strict_types=1);

namespace Tests\Feature\Internal;

use App\Events\Admin\AdminSurfaceUpdated;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Phase 2 — generic admin surface bridge.
 *
 * Locks the invariants:
 *   - Service-key auth required
 *   - Surface allow-list enforced (unknown surfaces 422)
 *   - affected_props is required + non-empty
 *   - surface_id is optional; both shapes dispatch AdminSurfaceUpdated
 *   - Payload field is optional, defaults to []
 */
final class AdminSurfaceUpdatedBridgeControllerTest extends TestCase
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
            'surface' => 'workflow-runs',
            'affected_props' => ['workflow_runs'],
        ], $overrides);
    }

    public function test_list_page_payload_dispatches_event_with_null_surface_id(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/admin-surface-updated',
                $this->payload([
                    'surface' => 'ml-training',
                    'affected_props' => ['runs'],
                    'payload' => ['workflow_kind' => 'train_target_model'],
                ]),
            )
            ->assertOk()
            ->assertJsonPath('ok', true);

        Event::assertDispatched(
            AdminSurfaceUpdated::class,
            function (AdminSurfaceUpdated $e): bool {
                return $e->surface === 'ml-training'
                    && $e->surfaceId === null
                    && $e->affectedProps === ['runs']
                    && ($e->payload['workflow_kind'] ?? null) === 'train_target_model';
            },
        );
    }

    public function test_drilldown_payload_with_surface_id_dispatches_per_resource(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        $runId = (string) Str::uuid();

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/admin-surface-updated',
                $this->payload([
                    'surface' => 'target-run',
                    'surface_id' => $runId,
                    'affected_props' => ['run'],
                ]),
            )
            ->assertOk();

        Event::assertDispatched(
            AdminSurfaceUpdated::class,
            fn (AdminSurfaceUpdated $e) => $e->surface === 'target-run'
                && $e->surfaceId === $runId
                && $e->affectedProps === ['run'],
        );
    }

    public function test_unknown_surface_is_rejected(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/admin-surface-updated',
                $this->payload(['surface' => 'not-a-real-surface']),
            )
            ->assertStatus(422);

        Event::assertNotDispatched(AdminSurfaceUpdated::class);
    }

    public function test_empty_affected_props_is_rejected(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/admin-surface-updated',
                $this->payload(['affected_props' => []]),
            )
            ->assertStatus(422);

        Event::assertNotDispatched(AdminSurfaceUpdated::class);
    }

    public function test_missing_service_key_is_rejected(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        $this->postJson('/api/internal/v1/admin-surface-updated', $this->payload())
            ->assertUnauthorized();

        Event::assertNotDispatched(AdminSurfaceUpdated::class);
    }

    public function test_payload_field_is_optional(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson(
                '/api/internal/v1/admin-surface-updated',
                ['surface' => 'reports', 'affected_props' => ['builds']],
            )
            ->assertOk();

        Event::assertDispatched(
            AdminSurfaceUpdated::class,
            fn (AdminSurfaceUpdated $e) => $e->surface === 'reports' && $e->payload === [],
        );
    }

    public function test_all_allowlisted_surfaces_round_trip(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        $surfaces = [
            'workflow-runs',
            'cluster-ingest',
            'target-recommendation',
            'target-run',
            'reports',
            'ml-training',
            'audit-findings',
            'alerts-inbox',
            'ingestion-review',
            // Phase 3 additions
            'support-cockpit',
            'llm-cost',
            // Phase 5 additions
            'cache-telemetry',
            'eval-dashboard',
            'conflicts',
            'audit-explorer',
            'backups',
            'integrations',
            'export-gate',
            'decision-history',
            'hypothesis-workspace',
            'what-changed',
            'source-trust',
        ];

        foreach ($surfaces as $surface) {
            $this->withHeaders(['X-Service-Key' => $this->serviceKey])
                ->postJson(
                    '/api/internal/v1/admin-surface-updated',
                    ['surface' => $surface, 'affected_props' => ['x']],
                )
                ->assertOk();
        }

        Event::assertDispatchedTimes(AdminSurfaceUpdated::class, count($surfaces));
    }
}
