<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Azure;

use App\Services\Azure\ManagedIdentityTokenProvider;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

class ManagedIdentityTokenProviderTest extends TestCase
{
    public function test_fetches_and_caches_a_token_from_imds(): void
    {
        Cache::forget('azure:imds_token:storage');
        Http::fake([
            'http://169.254.169.254/*' => Http::response([
                'access_token' => 'fake-imds-token',
                'expires_on' => (string) (time() + 3600),
            ], 200),
        ]);

        $provider = app(ManagedIdentityTokenProvider::class);
        $token = $provider->getToken();

        $this->assertSame('fake-imds-token', $token);
        Http::assertSent(function ($request) {
            return $request->hasHeader('Metadata', 'true')
                && str_contains($request->url(), 'resource=https%3A%2F%2Fstorage.azure.com%2F');
        });
    }

    public function test_second_call_uses_the_cache_and_does_not_refetch(): void
    {
        Cache::forget('azure:imds_token:storage');
        Cache::put('azure:imds_token:storage', 'cached-token', 3600);
        Http::fake();

        $provider = app(ManagedIdentityTokenProvider::class);
        $token = $provider->getToken();

        $this->assertSame('cached-token', $token);
        Http::assertNothingSent();
    }

    public function test_throws_on_a_failed_imds_response(): void
    {
        Cache::forget('azure:imds_token:storage');
        Http::fake([
            'http://169.254.169.254/*' => Http::response('forbidden', 403),
        ]);

        $provider = app(ManagedIdentityTokenProvider::class);

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/IMDS token request failed/');
        $provider->getToken();
    }
}
