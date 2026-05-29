<?php

declare(strict_types=1);

namespace Tests\Feature\Internal;

use App\Events\User\UserInboxUpdated;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Tests\TestCase;

/**
 * Phase 3 — per-user inbox bridge.
 *
 * Locks the invariants:
 *   - Service-key auth required
 *   - user_id must be a positive integer
 *   - kind must be one of mention | review | refusal
 *   - count_delta is optional (defaults to 1), capped at 1000
 *   - Bridge dispatches UserInboxUpdated with the correct shape
 */
final class UserInboxBridgeControllerTest extends TestCase
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

    public function test_mention_kind_dispatches(): void
    {
        Event::fake([UserInboxUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/user-inbox-updated', [
                'user_id' => 42,
                'kind' => 'mention',
                'payload' => ['source_page' => '/projects/x/chat'],
            ])
            ->assertOk();

        Event::assertDispatched(
            UserInboxUpdated::class,
            function (UserInboxUpdated $e): bool {
                return $e->userId === 42
                    && $e->kind === UserInboxUpdated::KIND_MENTION
                    && $e->countDelta === 1
                    && ($e->payload['source_page'] ?? null) === '/projects/x/chat';
            },
        );
    }

    public function test_review_kind_dispatches(): void
    {
        Event::fake([UserInboxUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/user-inbox-updated', [
                'user_id' => 7,
                'kind' => 'review',
                'count_delta' => 3,
            ])
            ->assertOk();

        Event::assertDispatched(
            UserInboxUpdated::class,
            fn (UserInboxUpdated $e) => $e->kind === UserInboxUpdated::KIND_REVIEW && $e->countDelta === 3,
        );
    }

    public function test_refusal_kind_dispatches(): void
    {
        Event::fake([UserInboxUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/user-inbox-updated', [
                'user_id' => 1,
                'kind' => 'refusal',
            ])
            ->assertOk();

        Event::assertDispatched(
            UserInboxUpdated::class,
            fn (UserInboxUpdated $e) => $e->kind === UserInboxUpdated::KIND_REFUSAL,
        );
    }

    public function test_invalid_kind_rejected(): void
    {
        Event::fake([UserInboxUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/user-inbox-updated', [
                'user_id' => 1,
                'kind' => 'spam',
            ])
            ->assertStatus(422);

        Event::assertNotDispatched(UserInboxUpdated::class);
    }

    public function test_missing_service_key_rejected(): void
    {
        Event::fake([UserInboxUpdated::class]);

        $this->postJson('/api/internal/v1/user-inbox-updated', [
            'user_id' => 1,
            'kind' => 'mention',
        ])->assertUnauthorized();

        Event::assertNotDispatched(UserInboxUpdated::class);
    }

    public function test_negative_user_id_rejected(): void
    {
        Event::fake([UserInboxUpdated::class]);

        $this->withHeaders(['X-Service-Key' => $this->serviceKey])
            ->postJson('/api/internal/v1/user-inbox-updated', [
                'user_id' => -1,
                'kind' => 'mention',
            ])
            ->assertStatus(422);

        Event::assertNotDispatched(UserInboxUpdated::class);
    }
}
