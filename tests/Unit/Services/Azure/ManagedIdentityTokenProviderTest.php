<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Azure;

use App\Services\Azure\ManagedIdentityTokenProvider;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

/**
 * Container Apps injects IDENTITY_ENDPOINT/IDENTITY_HEADER (App-Service-style
 * managed identity), NOT the VM IMDS endpoint (169.254.169.254) — a first
 * implementation assumed IMDS and got a real "could not connect" failure
 * against a live deployment. See ManagedIdentityTokenProvider's docblock.
 */
class ManagedIdentityTokenProviderTest extends TestCase
{
    private function setIdentityEnv(): void
    {
        putenv('IDENTITY_ENDPOINT=http://localhost:12356/msi/token');
        putenv('IDENTITY_HEADER=fake-identity-header-secret');
    }

    protected function tearDown(): void
    {
        putenv('IDENTITY_ENDPOINT');
        putenv('IDENTITY_HEADER');
        parent::tearDown();
    }

    public function test_fetches_and_caches_a_token_from_the_identity_endpoint(): void
    {
        Cache::forget('azure:msi_token:storage');
        Cache::forget('azure:msi_token:storage:expires_at');
        $this->setIdentityEnv();
        Http::fake([
            'http://localhost:12356/*' => Http::response([
                'access_token' => 'fake-msi-token',
                'expires_on' => (string) (time() + 3600),
            ], 200),
        ]);

        $provider = app(ManagedIdentityTokenProvider::class);
        $token = $provider->getToken();

        $this->assertSame('fake-msi-token', $token);
        Http::assertSent(function ($request) {
            return $request->hasHeader('X-IDENTITY-HEADER', 'fake-identity-header-secret')
                && str_contains($request->url(), 'resource=https%3A%2F%2Fstorage.azure.com%2F');
        });
    }

    public function test_second_call_uses_the_cache_and_does_not_refetch(): void
    {
        Cache::forget('azure:msi_token:storage');
        Cache::forget('azure:msi_token:storage:expires_at');
        // A warm cache is BOTH keys. The provider now returns the token and
        // its expiry together, because callers that bake the token into a
        // long-lived object (the Azure blob disk) need to know when their
        // copy dies — see App\Services\Azure\AzureBlobDiskLifetime. Seeding
        // only the token leaves the cache half-written, which the provider
        // deliberately treats as a miss rather than handing back a token
        // with no known lifetime.
        Cache::put('azure:msi_token:storage', 'cached-token', 3600);
        Cache::put('azure:msi_token:storage:expires_at', time() + 3600, 3600);
        $this->setIdentityEnv();
        Http::fake();

        $provider = app(ManagedIdentityTokenProvider::class);
        $token = $provider->getToken();

        $this->assertSame('cached-token', $token);
        Http::assertNothingSent();
    }

    public function test_throws_on_a_failed_identity_endpoint_response(): void
    {
        Cache::forget('azure:msi_token:storage');
        Cache::forget('azure:msi_token:storage:expires_at');
        $this->setIdentityEnv();
        Http::fake([
            'http://localhost:12356/*' => Http::response('forbidden', 403),
        ]);

        $provider = app(ManagedIdentityTokenProvider::class);

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/Managed-identity token request failed/');
        $provider->getToken();
    }

    public function test_throws_a_clear_error_when_no_managed_identity_is_configured(): void
    {
        Cache::forget('azure:msi_token:storage');
        Cache::forget('azure:msi_token:storage:expires_at');
        putenv('IDENTITY_ENDPOINT');
        putenv('IDENTITY_HEADER');

        $provider = app(ManagedIdentityTokenProvider::class);

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/no managed identity enabled/');
        $provider->getToken();
    }
}
