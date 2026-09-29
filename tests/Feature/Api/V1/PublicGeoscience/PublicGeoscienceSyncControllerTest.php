<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Http\Controllers\Api\V1\PublicGeoscience\PublicGeoscienceSyncController;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\Client\Request as HttpRequest;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

/**
 * POST /api/v1/public-geoscience/sync and GET .../sync-status.
 *
 * FastAPI is faked with Http::fake — these assert the admin gate, the exact
 * outbound call (URL, both credentials, payload), the cooldown guard, and
 * how each FastAPI failure maps to the Laravel response.
 */
final class PublicGeoscienceSyncControllerTest extends TestCase
{
    use RefreshDatabase;

    private const TRIGGER_URL = 'http://fastapi.test/internal/v1/public-geo/sync/trigger';

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long',
            'services.fastapi.internal_url' => 'http://fastapi.test',
        ]);
        Cache::forget(PublicGeoscienceSyncController::COOLDOWN_KEY);
    }

    /**
     * @param list<string>|null $codes
     */
    private function fakeAccepted(string $runId = 'run-abc', ?array $codes = null, int $feeds = 28): void
    {
        Http::fake([
            self::TRIGGER_URL => Http::response([
                'workflow_run_id' => $runId,
                'workflow' => 'public_geo_sync',
                'jurisdiction_codes' => $codes,
                'feeds' => $feeds,
            ], 202),
        ]);
    }

    public function test_requires_authentication(): void
    {
        $this->postJson('/api/v1/public-geoscience/sync')->assertUnauthorized();
    }

    public function test_non_admin_is_forbidden_and_nothing_is_dispatched(): void
    {
        Http::fake();

        $this->actingAs(User::factory()->create())
            ->postJson('/api/v1/public-geoscience/sync')
            ->assertForbidden();

        Http::assertNothingSent();
    }

    public function test_admin_trigger_calls_fastapi_with_both_credentials_and_returns_the_run_id(): void
    {
        $this->fakeAccepted('run-123', ['CA-SK'], 28);
        $admin = User::factory()->admin()->create();

        $this->actingAs($admin)
            ->postJson('/api/v1/public-geoscience/sync', ['jurisdiction_codes' => ['CA-SK', 'CA-SK']])
            ->assertStatus(202)
            ->assertJson(['workflow_run_id' => 'run-123', 'jurisdiction_codes' => ['CA-SK'], 'feeds' => 28]);

        Http::assertSent(function (HttpRequest $request) use ($admin): bool {
            return $request->url() === self::TRIGGER_URL
                && $request->method() === 'POST'
                && $request->hasHeader('X-Service-Key', 'test-service-key-must-be-at-least-32-bytes-long')
                && str_starts_with($request->header('Authorization')[0] ?? '', 'Bearer ')
                && $request['jurisdiction_codes'] === ['CA-SK']
                && $request['requested_by'] === 'web:'.$admin->email;
        });
    }

    public function test_all_jurisdictions_sends_no_filter(): void
    {
        $this->fakeAccepted();

        $this->actingAs(User::factory()->admin()->create())
            ->postJson('/api/v1/public-geoscience/sync')
            ->assertStatus(202);

        Http::assertSent(fn (HttpRequest $request): bool => ! array_key_exists('jurisdiction_codes', $request->data()));
    }

    public function test_second_trigger_within_the_cooldown_is_429_with_the_first_run_id(): void
    {
        $this->fakeAccepted('run-first');
        $admin = User::factory()->admin()->create();

        $this->actingAs($admin)->postJson('/api/v1/public-geoscience/sync')->assertStatus(202);
        $this->actingAs($admin)
            ->postJson('/api/v1/public-geoscience/sync')
            ->assertStatus(429)
            ->assertJson(['error' => 'sync_recently_triggered', 'workflow_run_id' => 'run-first']);

        Http::assertSentCount(1);
    }

    public function test_fastapi_422_is_passed_through_and_releases_the_cooldown(): void
    {
        Http::fake([
            self::TRIGGER_URL => Http::sequence()
                ->push(['detail' => "no public-geo feeds are registered for ['CA-AB']"], 422)
                ->push(['workflow_run_id' => 'run-retry', 'jurisdiction_codes' => null, 'feeds' => 28], 202),
        ]);
        $admin = User::factory()->admin()->create();

        $this->actingAs($admin)
            ->postJson('/api/v1/public-geoscience/sync', ['jurisdiction_codes' => ['CA-AB']])
            ->assertStatus(422)
            ->assertJsonPath('error', 'trigger_failed');

        // Nothing was dispatched, so an immediate retry must not hit the cooldown.
        $this->actingAs($admin)
            ->postJson('/api/v1/public-geoscience/sync')
            ->assertStatus(202)
            ->assertJsonPath('workflow_run_id', 'run-retry');
    }

    public function test_fastapi_unreachable_is_502(): void
    {
        Http::fake(fn () => throw new ConnectionException('Connection refused'));

        $this->actingAs(User::factory()->admin()->create())
            ->postJson('/api/v1/public-geoscience/sync')
            ->assertStatus(502)
            ->assertJsonPath('error', 'trigger_failed');
    }

    public function test_fastapi_5xx_is_502(): void
    {
        Http::fake([self::TRIGGER_URL => Http::response('boom', 500)]);

        $this->actingAs(User::factory()->admin()->create())
            ->postJson('/api/v1/public-geoscience/sync')
            ->assertStatus(502);
    }

    public function test_malformed_jurisdiction_is_rejected_before_any_call(): void
    {
        Http::fake();

        $this->actingAs(User::factory()->admin()->create())
            ->postJson('/api/v1/public-geoscience/sync', ['jurisdiction_codes' => ["CA-SK'; DROP"]])
            ->assertUnprocessable();

        Http::assertNothingSent();
    }

    public function test_sync_status_is_readable_by_any_signed_in_user(): void
    {
        // SQLite has no public_geo schema; the endpoint answers an empty
        // status there rather than erroring. The per-layer SQL is exercised
        // on the pgsql suite (PublicGeoscienceMapPolygonLayersTest seeds the
        // same tables).
        $this->actingAs(User::factory()->create())
            ->getJson('/api/v1/public-geoscience/sync-status')
            ->assertOk()
            ->assertJsonStructure(['layers', 'last_seen_at']);
    }
}
