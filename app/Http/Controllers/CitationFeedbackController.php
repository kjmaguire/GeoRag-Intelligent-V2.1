<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;

/**
 * POST /api/v1/citations/feedback — record a citation thumbs-up/down.
 *
 * Backs the 👍/👎 buttons in ChatMessage. Proxies to FastAPI's
 * /api/v1/citations/feedback endpoint, which writes a row into
 * silver.source_trust_features.
 */
class CitationFeedbackController extends Controller
{
    public function submit(Request $request): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'answer_run_id' => ['required', 'uuid'],
            'citation_item_id' => ['required', 'uuid'],
            'source_document_id' => ['required', 'uuid'],
            'verdict' => ['required', 'in:wrong,right,partial'],
            'reason' => ['nullable', 'string', 'max:2000'],
        ]);
        $payload['submitted_by_user_id'] = $user->id;

        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            return response()->json(['error' => 'FASTAPI_SERVICE_KEY not configured'], 500);
        }
        $base = rtrim(
            config('services.fastapi.internal_url')
                ?? config('services.fastapi.internal_url'),
            '/',
        );

        try {
            $response = Http::withHeaders(['X-Service-Key' => $serviceKey])
                ->timeout(10)
                ->post($base.'/api/v1/citations/feedback', $payload);
        } catch (\Throwable $exc) {
            return response()->json(
                ['error' => 'fastapi dispatch failed', 'reason' => $exc->getMessage()],
                502,
            );
        }

        if (! $response->ok()) {
            return response()->json(
                ['error' => 'fastapi returned non-2xx',
                 'fastapi_status' => $response->status(),
                 'fastapi_body' => $response->json()],
                502,
            );
        }

        return response()->json($response->json(), 201);
    }
}
