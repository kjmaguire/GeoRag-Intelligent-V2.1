<?php

declare(strict_types=1);

namespace App\Services\Azure;

use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use RuntimeException;

/**
 * Fetches an Azure AD access token for a Container App's system-assigned
 * managed identity, so blob storage auth needs no long-lived stored
 * secret at all.
 *
 * Container Apps uses the App-Service-style managed-identity protocol —
 * a per-replica local endpoint at IDENTITY_ENDPOINT authenticated with
 * IDENTITY_HEADER — NOT the classic VM IMDS endpoint
 * (169.254.169.254/metadata/identity/...). That distinction was
 * confirmed live: a first implementation hardcoded the VM IMDS URL and
 * got "Could not connect to server" against a real deployment, since
 * Container Apps has no route to 169.254.169.254 at all. Both env vars
 * are injected by the platform at container runtime for any app with a
 * system-assigned (or user-assigned) identity enabled — see
 * https://learn.microsoft.com/azure/container-apps/managed-identity.
 *
 * Cached in Redis (not a static/instance property) — Octane keeps this
 * service alive across requests on the same worker, and the token is
 * shared across every Octane worker and every replica, so a
 * process-local cache would mean redundant token fetches per worker while
 * a Redis cache means the whole fleet fetches once per ~1h token lifetime.
 */
class ManagedIdentityTokenProvider
{
    private const RESOURCE = 'https://storage.azure.com/';

    private const CACHE_KEY = 'azure:msi_token:storage';

    /**
     * Returns a valid bearer token, fetching a fresh one if the cached
     * token is missing or within 5 minutes of its actual expiry (tokens
     * are typically valid ~24h; refreshing early avoids a request failing
     * mid-flight on a token that expires between cache-read and use).
     */
    public function getToken(): string
    {
        $cached = Cache::get(self::CACHE_KEY);
        if (is_string($cached) && $cached !== '') {
            return $cached;
        }

        $endpoint = (string) env('IDENTITY_ENDPOINT');
        $header = (string) env('IDENTITY_HEADER');
        if ($endpoint === '' || $header === '') {
            throw new RuntimeException(
                'IDENTITY_ENDPOINT/IDENTITY_HEADER are not set — this Container App has no '
                .'managed identity enabled (az containerapp identity assign --system-assigned).',
            );
        }

        $response = Http::withHeaders(['X-IDENTITY-HEADER' => $header])
            ->timeout(10)
            ->get($endpoint, [
                'api-version' => '2019-08-01',
                'resource' => self::RESOURCE,
            ]);

        if (! $response->successful()) {
            throw new RuntimeException(
                "Managed-identity token request failed: HTTP {$response->status()} — {$response->body()}",
            );
        }

        $token = $response->json('access_token');
        $expiresOn = (int) $response->json('expires_on', 0);

        if (! is_string($token) || $token === '') {
            throw new RuntimeException('Managed-identity token response had no access_token field.');
        }

        $ttl = max(60, $expiresOn - time() - 300);
        Cache::put(self::CACHE_KEY, $token, $ttl);

        return $token;
    }
}
