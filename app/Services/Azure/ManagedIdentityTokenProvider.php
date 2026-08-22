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
     * Companion key holding the absolute expiry (unix seconds) of whatever
     * token CACHE_KEY currently holds.
     *
     * Callers that cache something built AROUND the token — the Azure blob
     * disk does, because microsoft/azure-storage-blob takes the bearer as a
     * constructor string and offers no way to refresh it — need to know
     * when their copy goes stale. Without this they cannot tell, and the
     * careful early-refresh below is wasted on them: they ask once, hold
     * the answer forever, and start getting 401s at a time nothing here
     * can observe.
     */
    private const EXPIRY_CACHE_KEY = 'azure:msi_token:storage:expires_at';

    /**
     * Returns a valid bearer token, fetching a fresh one if the cached
     * token is missing or within 5 minutes of its actual expiry (tokens
     * are typically valid ~24h; refreshing early avoids a request failing
     * mid-flight on a token that expires between cache-read and use).
     */
    public function getToken(): string
    {
        return $this->getTokenWithExpiry()[0];
    }

    /**
     * The bearer token and the unix timestamp after which it must not be
     * used.
     *
     * The expiry is the cache TTL boundary, not the raw `expires_on` from
     * Azure: it already has the 5-minute safety margin subtracted, so a
     * caller holding a derived object can treat it as "rebuild at or after
     * this instant" without redoing that arithmetic.
     *
     * @return array{0: string, 1: int}
     */
    public function getTokenWithExpiry(): array
    {
        $cached = Cache::get(self::CACHE_KEY);
        $cachedExpiry = Cache::get(self::EXPIRY_CACHE_KEY);
        if (is_string($cached) && $cached !== '' && is_int($cachedExpiry)) {
            return [$cached, $cachedExpiry];
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
        $expiresAt = time() + $ttl;
        Cache::put(self::CACHE_KEY, $token, $ttl);
        // Same TTL, so the two keys expire together and a caller can never
        // read a token without its expiry or vice versa.
        Cache::put(self::EXPIRY_CACHE_KEY, $expiresAt, $ttl);

        return [$token, $expiresAt];
    }
}
