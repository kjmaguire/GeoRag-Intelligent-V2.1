<?php

declare(strict_types=1);

namespace Tests\Feature\Console;

use Illuminate\Http\Client\Request as HttpRequest;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

/**
 * `php artisan public-geo:sync` — same trigger path as the admin button.
 */
final class PublicGeoSyncCommandTest extends TestCase
{
    private const TRIGGER_URL = 'http://fastapi.test/internal/v1/public-geo/sync/trigger';

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long',
            'services.fastapi.internal_url' => 'http://fastapi.test',
        ]);
    }

    public function test_queues_a_run_for_all_jurisdictions(): void
    {
        Http::fake([self::TRIGGER_URL => Http::response(
            ['workflow_run_id' => 'run-cli', 'jurisdiction_codes' => null, 'feeds' => 28], 202,
        )]);

        $this->artisan('public-geo:sync')
            ->expectsOutputToContain('Queued public_geo_sync run run-cli')
            ->expectsOutputToContain('jurisdictions: all · feeds: 28')
            ->assertSuccessful();

        Http::assertSent(fn (HttpRequest $r): bool => $r->hasHeader('X-Service-Key')
            && ! array_key_exists('jurisdiction_codes', $r->data())
            && str_starts_with((string) $r['requested_by'], 'cli:'));
    }

    public function test_repeatable_jurisdiction_and_max_features(): void
    {
        Http::fake([self::TRIGGER_URL => Http::response(
            ['workflow_run_id' => 'run-sk', 'jurisdiction_codes' => ['CA-SK'], 'feeds' => 28], 202,
        )]);

        $this->artisan('public-geo:sync', ['--jurisdiction' => ['ca-sk', ' CA-SK '], '--max-features' => '25'])
            ->expectsOutputToContain('jurisdictions: CA-SK · feeds: 28')
            ->assertSuccessful();

        Http::assertSent(fn (HttpRequest $r): bool => $r['jurisdiction_codes'] === ['CA-SK']
            && $r['max_features_per_source'] === 25);
    }

    public function test_invalid_jurisdiction_is_rejected_without_a_call(): void
    {
        Http::fake();

        $this->artisan('public-geo:sync', ['--jurisdiction' => ['Saskatchewan']])
            ->expectsOutputToContain('Invalid jurisdiction code')
            ->assertExitCode(2);

        Http::assertNothingSent();
    }

    public function test_invalid_max_features_is_rejected(): void
    {
        Http::fake();

        $this->artisan('public-geo:sync', ['--max-features' => '0'])->assertExitCode(2);

        Http::assertNothingSent();
    }

    public function test_fastapi_refusal_fails_the_command(): void
    {
        Http::fake([self::TRIGGER_URL => Http::response(['detail' => 'no public-geo feeds are registered'], 422)]);

        $this->artisan('public-geo:sync', ['--jurisdiction' => ['CA-AB']])
            ->expectsOutputToContain('no public-geo feeds are registered')
            ->assertFailed();
    }
}
