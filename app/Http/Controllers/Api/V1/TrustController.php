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
                ?? env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
            '/',
        );
        $serviceKey = config('services.fastapi.service_key')
            ?? env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            return response()->json(['error' => 'fastapi service key missing'], 500);
        }

        $projectId = (string) $request->query('project_id', '');
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
