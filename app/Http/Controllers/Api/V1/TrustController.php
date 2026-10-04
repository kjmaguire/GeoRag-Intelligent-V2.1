<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Services\FastApiJwtMinter;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Str;

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
 * The project is derived from silver.answer_runs, not from the request;
 * a `project_id` query parameter, if a client still sends one, is ignored.
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
            config('services.fastapi.internal_url'),
            '/',
        );
        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            return response()->json(['error' => 'fastapi service key missing'], 500);
        }

        // Tenancy gate. The project is resolved from the answer run itself,
        // NEVER from the query string. `?project_id=` used to be authorised
        // as given and then forwarded in the JWT, so a member of project A
        // could name A and a run id belonging to sibling project B in the
        // same workspace and read B's trust summary. Same lookup as
        // AnswerRunFeedbackController::store().
        if (! Str::isUuid($answerRunId)) {
            return response()->json(['error' => 'not_found'], 404);
        }
        $answerRun = DB::table('silver.answer_runs')
            ->where('answer_run_id', $answerRunId)
            ->select('project_id', 'workspace_id')
            ->first();
        if ($answerRun === null
            || $answerRun->project_id === null
            || ! $user->hasProjectAccess((string) $answerRun->project_id)
        ) {
            return response()->json(['error' => 'not_found'], 404);
        }
        $projectId = (string) $answerRun->project_id;
        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $user->id,
            $projectId,
            [],
            $answerRun->workspace_id !== null ? (string) $answerRun->workspace_id : null,
        );

        try {
            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(
                2,
                250,
                fn (\Throwable $exc): bool => $exc instanceof ConnectionException,
                throw: false,
            )->get(
                $fastApiBase.'/v1/answer_runs/'.rawurlencode($answerRunId).'/trust-summary',
            );
        } catch (\Throwable) {
            return response()->json(['error' => 'fastapi unreachable'], 502);
        }

        if (! $resp->ok()) {
            // FastAPI's 401/403/419 describe the Laravel->FastAPI service
            // credential, not the browser's session. Passed through, the
            // SPA's global fetch wrapper reads them as "session expired" and
            // logs the user out. Map them to a neutral 502.
            if (in_array($resp->status(), [401, 403, 419], true)) {
                return response()->json([
                    'error' => 'upstream_unavailable',
                    'message' => 'The analysis service could not complete this request.',
                ], 502);
            }

            return response()->json([
                'error' => 'fastapi non-2xx',
                'status' => $resp->status(),
                'body' => $resp->json() ?? $resp->body(),
            ], $resp->status() >= 400 && $resp->status() < 600 ? $resp->status() : 502);
        }

        return response()->json($resp->json());
    }
}
