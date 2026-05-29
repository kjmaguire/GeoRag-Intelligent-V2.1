<?php

declare(strict_types=1);

namespace Tests\Feature\Internal;

use App\Events\Map\PublicGeoscienceTilesInvalidated;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Tests\TestCase;

/**
 * Phase 4 — Public-Geoscience tile invalidation bridge.
 *
 * Locks the invariants:
 *   - Service-key auth required
 *   - jurisdiction_epoch must be a non-negative integer
 *   - source_ids is optional (null = invalidate all)
 *   - Bridge dispatches PublicGeoscienceTilesInvalidated with the
 *     correct payload shape
 */
final class PublicGeoscienceTilesInvalidatedBridgeControllerTest extends TestCase
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

    public function test_basic_payload_dispatches_event(): void
    {
        Event::fake([PublicGeoscienceTilesInvalidated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/public-geoscience-tiles-invalidated', [
                'jurisdiction_epoch' => 1716578400,
            ])
            ->assertOk()
            ->assertJsonPath('ok', true);

        Event::assertDispatched(
            PublicGeoscienceTilesInvalidated::class,
            function (PublicGeoscienceTilesInvalidated $e): bool {
                return $e->jurisdictionEpoch === 1716578400
                    && $e->sourceIds === null;
            },
        );
    }

    public function test_source_ids_subset_passes_through(): void
    {
        Event::fake([PublicGeoscienceTilesInvalidated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/public-geoscience-tiles-invalidated', [
                'jurisdiction_epoch' => 1716578400,
                'source_ids' => ['pg_mines', 'pg_drillhole_collars'],
            ])
            ->assertOk();

        Event::assertDispatched(
            PublicGeoscienceTilesInvalidated::class,
            fn (PublicGeoscienceTilesInvalidated $e) => $e->sourceIds === ['pg_mines', 'pg_drillhole_collars'],
        );
    }

    public function test_missing_service_key_is_rejected(): void
    {
        Event::fake([PublicGeoscienceTilesInvalidated::class]);

        $this->postJson('/api/internal/v1/public-geoscience-tiles-invalidated', [
            'jurisdiction_epoch' => 1716578400,
        ])->assertUnauthorized();

        Event::assertNotDispatched(PublicGeoscienceTilesInvalidated::class);
    }

    public function test_negative_epoch_is_rejected(): void
    {
        Event::fake([PublicGeoscienceTilesInvalidated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/public-geoscience-tiles-invalidated', [
                'jurisdiction_epoch' => -1,
            ])
            ->assertStatus(422);

        Event::assertNotDispatched(PublicGeoscienceTilesInvalidated::class);
    }

    public function test_missing_epoch_is_rejected(): void
    {
        Event::fake([PublicGeoscienceTilesInvalidated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/public-geoscience-tiles-invalidated', [])
            ->assertStatus(422);

        Event::assertNotDispatched(PublicGeoscienceTilesInvalidated::class);
    }

    public function test_event_payload_contract(): void
    {
        // Lock the wire shape that the React hook depends on.
        Event::fake([PublicGeoscienceTilesInvalidated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/public-geoscience-tiles-invalidated', [
                'jurisdiction_epoch' => 42,
                'source_ids' => ['smdi_deposits'],
            ])
            ->assertOk();

        Event::assertDispatched(
            PublicGeoscienceTilesInvalidated::class,
            function (PublicGeoscienceTilesInvalidated $e): bool {
                $body = $e->broadcastWith();

                return $e->broadcastAs() === 'public_geoscience.tiles_invalidated'
                    && $body['jurisdiction_epoch'] === 42
                    && $body['source_ids'] === ['smdi_deposits']
                    && isset($body['updated_at']);
            },
        );
    }
}
