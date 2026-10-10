<?php

declare(strict_types=1);

namespace App\Services;

use App\Models\User;
use GuzzleHttp\Exception\ConnectException;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Support\Facades\Http;
use Throwable;

/**
 * Enqueue one of the HAT-13 Hatchet workflows through FastAPI.
 *
 * Laravel has no Hatchet client; FastAPI does. This posts to
 * `POST {services.fastapi.internal_url}/internal/v1/workflows/{workflow}/trigger`
 * with the two credentials every Laravel→FastAPI call carries (the shared
 * `X-Service-Key` and a short-lived `FastApiJwtMinter` bearer), and returns
 * the workflow run id without waiting. Same shape as PublicGeoSyncTrigger.
 *
 * The caller has already authorised the user (WorkflowTriggerPolicy) and
 * built the input from the authenticated user, never from the request body.
 * FastAPI validates the input against the workflow's own model and checks
 * that everything it names lives inside `$workspaceId`.
 *
 * Octane safety: stateless. Config is read per call; the minter is itself
 * stateless.
 */
final class HatchetWorkflowTrigger
{
    public function __construct(private readonly FastApiJwtMinter $jwtMinter) {}

    /**
     * @param array<string, mixed> $input the workflow's input model, as JSON
     * @param string $jwtProject the JWT project claim: the project id when there
     *                           is one, otherwise a label for the call
     *
     * @return array{workflow: string, workflow_run_id: string, workspace_id: string|null}
     *
     * @throws HatchetWorkflowTriggerException when FastAPI is unreachable or refuses
     */
    public function trigger(
        string $workflow,
        ?string $workspaceId,
        array $input,
        User $actor,
        string $jwtProject,
    ): array {
        $serviceKey = config('services.fastapi.service_key');
        if (! is_string($serviceKey) || $serviceKey === '') {
            throw new HatchetWorkflowTriggerException('FASTAPI_SERVICE_KEY not configured', 500);
        }

        $url = rtrim((string) config('services.fastapi.internal_url'), '/')
            .'/internal/v1/workflows/'.rawurlencode($workflow).'/trigger';

        $jwt = $this->jwtMinter->mint(
            userId: $actor->getKey(),
            projectId: $jwtProject,
            roles: ['workflow:'.$workflow],
            workspaceId: $workspaceId,
        );

        $payload = [
            'workspace_id' => $workspaceId,
            'requested_by' => 'web:'.$actor->email,
            'input' => $input,
        ];

        // Retry only when the request provably never reached FastAPI: a POST
        // that dispatched a run and then timed out on the response must not
        // dispatch a second one. Laravel raises ConnectionException for EVERY
        // transport failure, a read timeout included, so the type alone proves
        // nothing; see neverReachedFastApi().
        try {
            $response = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(
                2,
                250,
                fn (Throwable $exc): bool => self::neverReachedFastApi($exc),
                throw: false,
            )->post($url, $payload);
        } catch (ConnectionException $exc) {
            throw new HatchetWorkflowTriggerException(
                'FastAPI unreachable: '.$exc->getMessage(),
                502,
                $exc,
                maybeDispatched: ! self::neverReachedFastApi($exc),
            );
        }

        if (! $response->successful()) {
            $detail = $response->json('detail');
            $message = is_string($detail) ? $detail : substr($response->body(), 0, 300);
            $status = in_array($response->status(), [404, 422], true) ? $response->status() : 502;

            throw new HatchetWorkflowTriggerException(
                'FastAPI refused the '.$workflow.' trigger (HTTP '.$response->status().'): '.$message,
                $status,
            );
        }

        $runId = $response->json('workflow_run_id');
        if (! is_string($runId) || $runId === '') {
            throw new HatchetWorkflowTriggerException('FastAPI response is missing workflow_run_id', 502);
        }

        return [
            'workflow' => $workflow,
            'workflow_run_id' => $runId,
            'workspace_id' => $workspaceId,
        ];
    }

    /**
     * cURL errors that can only happen before a single byte of the request is
     * written: CURLE_COULDNT_RESOLVE_HOST (6) and CURLE_COULDNT_CONNECT (7).
     * Not 28 (a timeout can fire after the request was sent), not 52 (empty
     * reply) or 56 (receive failure): those arrive after FastAPI had it.
     */
    private const CONNECT_PHASE_ERRNOS = [6, 7];

    /**
     * True only when the failure proves FastAPI never received the request.
     *
     * Laravel wraps every Guzzle transport failure in ConnectionException,
     * including a cURL "operation timed out" after the request was written,
     * so a retry keyed on the exception type re-posts requests that did
     * dispatch. The cURL errno is on the Guzzle ConnectException that
     * Laravel keeps as the previous exception.
     */
    public static function neverReachedFastApi(Throwable $exc): bool
    {
        if (! $exc instanceof ConnectionException) {
            return false;
        }

        $previous = $exc->getPrevious();
        if (! $previous instanceof ConnectException) {
            return false;
        }

        return in_array($previous->getHandlerContext()['errno'] ?? null, self::CONNECT_PHASE_ERRNOS, true);
    }
}
