<?php

declare(strict_types=1);

namespace App\Services\Azure;

use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use RuntimeException;

/**
 * Fetches an Azure AD access token for a Container App's system-assigned
 * managed identity via the platform's IMDS endpoint, so blob storage auth
 * needs no long-lived stored secret at all.
 *
 * Cached in Redis (not a static/instance property) — Octane keeps this
 * service alive across requests on the same worker, and IMDS tokens are
 * also shared across every Octane worker and every replica, so a
 * process-local cache would mean redundant token fetches per worker while
 * a Redis cache means the whole fleet fetches once per ~1h token lifetime.
 */
class ManagedIdentityTokenProvider
{
    private const IMDS_URL = 'http://169.254.169.254/metadata/identity/oauth2/token';

    private const RESOURCE = 'https://storage.azure.com/';

    private const CACHE_KEY = 'azure:imds_token:storage';

    /**
     * Returns a valid bearer token, fetching a fresh one if the cached
     * token is missing or within 5 minutes of its actual expiry (IMDS
     * tokens are typically valid ~24h; refreshing early avoids a request
     * failing mid-flight on a token that expires between cache-read and
     * use).
     */
    public function getToken(): string
    {
        $cached = Cache::get(self::CACHE_KEY);
        if (is_string($cached) && $cached !== '') {
            return $cached;
        }

        $response = Http::withHeaders(['Metadata' => 'true'])
            ->timeout(10)
            ->get(self::IMDS_URL, [
                'api-version' => '2019-08-01',
                'resource' => self::RESOURCE,
            ]);

        if (! $response->successful()) {
            throw new RuntimeException(
                "IMDS token request failed: HTTP {$response->status()} — {$response->body()}",
            );
        }

        $token = $response->json('access_token');
        $expiresOn = (int) $response->json('expires_on', 0);

        if (! is_string($token) || $token === '') {
            throw new RuntimeException('IMDS response had no access_token field.');
        }

        $ttl = max(60, $expiresOn - time() - 300);
        Cache::put(self::CACHE_KEY, $token, $ttl);

        return $token;
    }
}
