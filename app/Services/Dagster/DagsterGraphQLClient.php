<?php

declare(strict_types=1);

namespace App\Services\Dagster;

use Illuminate\Http\Client\ConnectionException;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Throwable;

/**
 * Minimal Dagster GraphQL client.
 *
 * Posts the `launchPipelineExecution` mutation against the default asset job
 * (`__ASSET_JOB`) with an asset-key selection so a single asset (or a small
 * group) can be materialised synchronously without waiting for the 5-minute
 * MinIO sensor poll.
 *
 * Why GraphQL, not the Python API: Laravel can't import dagster.Definitions
 * over the wire. Dagster's webserver exposes a stable GraphQL endpoint that
 * matches what the Dagit UI uses; we hit the same surface.
 *
 * Octane-safe: stateless. Each call creates a fresh Http client. No static
 * caches, no per-request singletons.
 *
 * Dispatch failures are non-fatal — the caller logs + continues so the
 * upload response isn't blocked. The MinIO sensor still picks up the
 * object on its next poll as a safety net.
 */
final class DagsterGraphQLClient
{
    /**
     * Launch an asset materialisation via the default asset job.
     *
     * @param string $assetKey e.g. 'silver_collars', 'silver_lithology'
     * @param array<string, mixed> $opsConfig Optional ops config (e.g. ['silver_collars' => ['config' => ['object_key' => 'drill-uploads/...']]])
     *
     * @return array{dispatched: bool, run_id: ?string, error: ?string}
     */
    public function launchAssetMaterialization(string $assetKey, array $opsConfig = []): array
    {
        $endpoint = $this->endpoint();
        if ($endpoint === null) {
            return [
                'dispatched' => false,
                'run_id' => null,
                'error' => 'dagster_graphql_url_not_configured',
            ];
        }

        $mutation = <<<'GRAPHQL'
            mutation LaunchPipelineExecution($executionParams: ExecutionParams!) {
              launchPipelineExecution(executionParams: $executionParams) {
                __typename
                ... on LaunchRunSuccess { run { runId } }
                ... on InvalidStepError { invalidStepKey }
                ... on InvalidOutputError { stepKey invalidOutputName }
                ... on PipelineConfigValidationInvalid { errors { message } }
                ... on PipelineNotFoundError { message }
                ... on PythonError { message }
              }
            }
            GRAPHQL;

        $variables = [
            'executionParams' => [
                'selector' => [
                    'repositoryLocationName' => config('services.dagster.location', 'georag_dagster'),
                    'repositoryName' => config('services.dagster.repository', '__repository__'),
                    'pipelineName' => '__ASSET_JOB',
                    'assetSelection' => [['path' => [$assetKey]]],
                ],
                'mode' => 'default',
                'runConfigData' => $opsConfig === [] ? new \stdClass : ['ops' => $opsConfig],
            ],
        ];

        try {
            $resp = Http::timeout((int) config('services.dagster.timeout', 10))
                ->acceptJson()
                ->asJson()
                ->post($endpoint, [
                    'query' => $mutation,
                    'variables' => $variables,
                ]);

            if (! $resp->successful()) {
                Log::warning('DagsterGraphQLClient: non-2xx from /graphql', [
                    'asset_key' => $assetKey,
                    'status' => $resp->status(),
                    'body' => $resp->body(),
                ]);

                return [
                    'dispatched' => false,
                    'run_id' => null,
                    'error' => 'http_'.$resp->status(),
                ];
            }

            $body = $resp->json();
            $payload = $body['data']['launchPipelineExecution'] ?? null;
            $typename = $payload['__typename'] ?? null;

            if ($typename === 'LaunchRunSuccess') {
                $runId = $payload['run']['runId'] ?? null;
                Log::info('DagsterGraphQLClient: launched', [
                    'asset_key' => $assetKey,
                    'run_id' => $runId,
                ]);

                return ['dispatched' => true, 'run_id' => $runId, 'error' => null];
            }

            // Any non-success __typename indicates a Dagster-side rejection.
            Log::warning('DagsterGraphQLClient: launch rejected', [
                'asset_key' => $assetKey,
                'typename' => $typename,
                'payload' => $payload,
                'errors' => $body['errors'] ?? null,
            ]);

            return [
                'dispatched' => false,
                'run_id' => null,
                'error' => $typename ?? 'unknown_dagster_error',
            ];
        } catch (ConnectionException $e) {
            Log::warning('DagsterGraphQLClient: connection failed', [
                'asset_key' => $assetKey,
                'error' => $e->getMessage(),
            ]);

            return ['dispatched' => false, 'run_id' => null, 'error' => 'connection_failed'];
        } catch (Throwable $e) {
            Log::warning('DagsterGraphQLClient: unexpected failure', [
                'asset_key' => $assetKey,
                'error' => $e->getMessage(),
            ]);

            return ['dispatched' => false, 'run_id' => null, 'error' => 'exception'];
        }
    }

    private function endpoint(): ?string
    {
        $base = config('services.dagster.url') ?? env('DAGSTER_GRAPHQL_URL');
        if (! is_string($base) || $base === '') {
            return null;
        }

        return rtrim($base, '/').'/graphql';
    }
}
