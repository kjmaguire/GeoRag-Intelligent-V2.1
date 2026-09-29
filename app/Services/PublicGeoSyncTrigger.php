<?php

declare(strict_types=1);

namespace App\Services;

use Illuminate\Http\Client\ConnectionException;
use Illuminate\Support\Facades\Http;

/**
 * Enqueue the `public_geo_sync` Hatchet workflow through FastAPI.
 *
 * Laravel has no Hatchet client; FastAPI does. This posts to
 * `POST {services.fastapi.internal_url}/internal/v1/public-geo/sync/trigger`
 * with the same two credentials every Laravel→FastAPI call carries — the
 * shared `X-Service-Key` and a short-lived `FastApiJwtMinter` Bearer token —
 * and returns the workflow run id without waiting for the (multi-hour) sync.
 *
 * Callers: PublicGeoscienceSyncController (admin-only "Sync now" on the Public
 * Geo page) and the `public-geo:sync` artisan command. It is the ONLY place
 * the trigger URL and payload shape live, so the button and the CLI cannot
 * drift apart.
 *
 * Octane safety: stateless. Config is read per call; the minter is itself
 * stateless.
 */
final class PublicGeoSyncTrigger
{
    /**
     * JWT project claim for a call that is not project-scoped. public_geo is
     * cross-tenant reference data; FastAPI's trigger route authorises on the
     * service key and does not read this claim.
     */
    private const JWT_PROJECT = 'public-geo';

    public function __construct(private readonly FastApiJwtMinter $jwtMinter) {}

    /**
     * @param list<string>|null $jurisdictionCodes null/empty = every jurisdiction
     *
     * @return array{workflow_run_id: string, jurisdiction_codes: list<string>|null, feeds: int}
     *
     * @throws PublicGeoSyncTriggerException when FastAPI is unreachable or refuses
     */
    public function trigger(
        ?array $jurisdictionCodes,
        int|string|null $actorId,
        string $requestedBy,
        ?int $maxFeaturesPerSource = null,
    ): array {
        $serviceKey = config('services.fastapi.service_key');
        if (! is_string($serviceKey) || $serviceKey === '') {
            throw new PublicGeoSyncTriggerException('FASTAPI_SERVICE_KEY not configured', 500);
        }

        $url = rtrim((string) config('services.fastapi.internal_url'), '/')
            .'/internal/v1/public-geo/sync/trigger';

        $jwt = $this->jwtMinter->mint(
            userId: $actorId ?? 0,
            projectId: self::JWT_PROJECT,
            roles: ['public_geo:sync'],
        );

        $payload = array_filter([
            'jurisdiction_codes' => $jurisdictionCodes ? array_values($jurisdictionCodes) : null,
            'max_features_per_source' => $maxFeaturesPerSource,
            'requested_by' => $requestedBy,
        ], static fn (mixed $v): bool => $v !== null);

        // Retry only when the request never reached FastAPI: a POST that
        // dispatched a run and then timed out on the response must not
        // dispatch a second one.
        try {
            $response = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(
                2,
                250,
                fn (\Throwable $exc): bool => $exc instanceof ConnectionException,
                throw: false,
            )->post($url, $payload);
        } catch (ConnectionException $exc) {
            throw new PublicGeoSyncTriggerException('FastAPI unreachable: '.$exc->getMessage(), 502, $exc);
        }

        if (! $response->successful()) {
            $detail = $response->json('detail');
            $message = is_string($detail) ? $detail : substr($response->body(), 0, 300);

            throw new PublicGeoSyncTriggerException(
                'FastAPI refused the sync trigger (HTTP '.$response->status().'): '.$message,
                $response->status() === 422 ? 422 : 502,
            );
        }

        $runId = $response->json('workflow_run_id');
        if (! is_string($runId) || $runId === '') {
            throw new PublicGeoSyncTriggerException('FastAPI response is missing workflow_run_id', 502);
        }

        /** @var list<string>|null $codes */
        $codes = $response->json('jurisdiction_codes');

        return [
            'workflow_run_id' => $runId,
            'jurisdiction_codes' => $codes,
            'feeds' => (int) $response->json('feeds', 0),
        ];
    }
}
