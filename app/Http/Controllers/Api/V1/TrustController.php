<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Services\FastApiJwtMinter;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;

/**
 * §19.2 Trust Inspector — Laravel-side proxy.
 *
 * The customer-chat surface (`Chat.tsx`) opens the Trust Inspector
 * drawer with a single answer_run_id. The drawer fetches the
 * aggregated 7-section payload from the FastAPI endpoint
 *   GET /v1/answer_runs/{id}/trust-summary
 * which requires a Laravel-minted JWT carrying the acting user +
 * project context.
 *
 * Authenticated via Sanctum; the JWT is minted on each call with a
 * short TTL (FastApiJwtMinter default).
 *
 * Route: GET /api/v1/answer-runs/{id}/trust-summary
 */
class TrustController extends Controller
{
    public function trustSummary(Request $request, string $answerRunId): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $fastApiBase = rtrim(
            config('services.fastapi.internal_url')
                ?? config('services.fastapi.internal_url'),
            '/',
        );
        $serviceKey = config('services.fastapi.service_key')
            ?? config('services.fastapi.service_key');
        if (! $serviceKey) {
            return response()->json(['error' => 'fastapi service key missing'], 500);
        }

        // Tenancy gate (Theme H extension — 2026-06-03 audit pass 5+++)
        //
        // Without this check, the controller minted a JWT with a caller-
        // supplied project_id and forwarded to FastAPI, which would then
        // resolve workspace_id from that project_id and serve the trust
        // summary for any answer_run that belonged to that workspace.
        // An attacker in workspace A could pass project_id=B (any project
        // they could name) and answer_run_id from B → cross-tenant read.
        //
        // Gate the JWT mint behind hasProjectAccess so the claimed
        // project_id always belongs to a project the caller can read.
        $projectId = (string) $request->query('project_id', '');
        if ($projectId === '' || ! $user->hasProjectAccess($projectId)) {
            return response()->json(['error' => 'not_found'], 404);
        }
        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $user->id,
            $projectId,
            [],
        );

        try {
            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->get(
                $fastApiBase.'/v1/answer_runs/'.$answerRunId.'/trust-summary',
            );
        } catch (\Throwable $exc) {
            return response()->json([
                'error' => 'fastapi unreachable',
                'reason' => $exc->getMessage(),
            ], 502);
        }

        if (! $resp->ok()) {
            return response()->json([
                'error' => 'fastapi non-2xx',
                'status' => $resp->status(),
                'body' => $resp->json() ?? $resp->body(),
            ], $resp->status());
        }

        return response()->json($resp->json());
    }
}
