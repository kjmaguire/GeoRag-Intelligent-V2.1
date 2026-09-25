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

/**
 * POST /api/v1/answer-runs/{answerRunId}/feedback — Laravel proxy for
 * FastAPI's POST /v1/answer_runs/{answer_run_id}/feedback.
 *
 * §10p / Module 7 Phase B Chunk 1 — 👍/👎 + optional taxonomy category +
 * free-text note on a settled assistant answer. `silver.message_feedback`
 * and the FastAPI writer (`src/fastapi/app/routers/answer_runs.py::post_feedback`)
 * have existed since 2026-04-22 with no Laravel caller (§10p as-built note,
 * georag-architecture.html). This controller is that caller — built
 * 2026-09-24, same shape as `EvidenceController::show()` (2026-06-03 audit
 * pass 6): resolve the answer_run's project_id/workspace_id from the DB,
 * gate on the authenticated user's project access, mint a short-TTL
 * FastAPI JWT, forward with the server-side service key. The service key
 * never reaches the browser.
 *
 * Polarity/category validation mirrors FastAPI's `FeedbackCreate` Pydantic
 * model exactly (same 6-value taxonomy, same "category required when
 * polarity=down" rule) so a request that fails Laravel's validation would
 * have failed FastAPI's too — the duplication is intentional, not drift.
 */
class AnswerRunFeedbackController extends Controller
{
    public function store(Request $request, string $answerRunId): JsonResponse
    {
        $user = $request->user();
        if ($user === null) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $payload = $request->validate([
            'polarity' => ['required', 'in:up,down'],
            'category' => [
                'nullable',
                'in:hallucinated,wrong_facts,missing_info,off_topic,citation_issue,length_issue',
                'required_if:polarity,down',
            ],
            'note' => ['nullable', 'string', 'max:2000'],
        ]);

        // Tenancy gate (same pattern as EvidenceController::show() /
        // CitationFeedbackController::submit() — resolve the parent row's
        // true project_id/workspace_id from the DB rather than trusting a
        // client-supplied value, then verify project access).
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

        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            return response()->json(['error' => 'fastapi service key missing'], 500);
        }
        $fastApiBase = rtrim(
            config('services.fastapi.internal_url'),
            '/',
        );

        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $user->id,
            (string) $answerRun->project_id,
            [],
            (string) $answerRun->workspace_id,
        );

        // Retry only when the request never reached FastAPI. This is a POST
        // that inserts a row, so retrying an answer it did give (a 5xx after
        // a partial write, a 4xx) could record the same feedback twice, and
        // the default retry() also throws on the last non-2xx, which turned
        // FastAPI's 400/422 into a 502 here.
        try {
            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(10)->retry(
                2,
                250,
                fn (\Throwable $exc): bool => $exc instanceof ConnectionException,
                throw: false,
            )->post(
                $fastApiBase.'/v1/answer_runs/'.rawurlencode($answerRunId).'/feedback',
                $payload,
            );
        } catch (ConnectionException) {
            return response()->json(['error' => 'fastapi unreachable'], 502);
        }

        // successful(), not ok(): FastAPI answers this POST with 201, and
        // ok() is true for exactly 200.
        if (! $resp->successful()) {
            return response()->json(
                ['error' => 'fastapi non-2xx', 'status' => $resp->status(), 'body' => $resp->json() ?? $resp->body()],
                $resp->status() >= 400 && $resp->status() < 600 ? $resp->status() : 502,
            );
        }

        return response()->json($resp->json(), 201);
    }
}
