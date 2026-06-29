<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * §8 Target Recommendation Cockpit (Phase H4 UI).
 *
 * Two pages:
 *   GET  /admin/target_recommendation/runs            — list of recent runs
 *   GET  /admin/target_recommendation/runs/{run_id}   — cockpit + R5 sign-off
 *
 * Sign-off endpoint:
 *   POST /admin/target_recommendation/runs/{run_id}/signoff
 *
 * Backed by FastAPI's `/api/v1/admin/target_recommendation/*` router.
 * Auth: 'admin' Gate.
 */
class TargetRecommendationCockpitController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $response = $this->fastapi()->get(
            $this->base().'/api/v1/admin/target_recommendation/runs',
            ['limit' => 50],
        );

        return Inertia::render('Admin/TargetRecommendationRuns', [
            'runs' => $response->ok() ? ($response->json('runs') ?? []) : [],
            'fastapi_error' => $response->ok() ? null : $response->body(),
        ]);
    }

    public function show(Request $request, string $run_id): Response
    {
        $this->authorize('admin');
        if (! preg_match('/^[0-9a-f-]{36}$/i', $run_id)) {
            abort(404, 'invalid run_id');
        }

        $response = $this->fastapi()->get(
            $this->base()."/api/v1/admin/target_recommendation/runs/{$run_id}",
        );

        if ($response->status() === 404) {
            abort(404, 'run not found');
        }
        if (! $response->ok()) {
            abort(502, 'fastapi error: '.$response->body());
        }

        return Inertia::render('Admin/TargetRecommendationCockpit', [
            'run' => $response->json(),
        ]);
    }

    public function geojson(Request $request, string $run_id): JsonResponse
    {
        $this->authorize('admin');
        if (! preg_match('/^[0-9a-f-]{36}$/i', $run_id)) {
            return response()->json(['error' => 'invalid run_id'], 400);
        }
        $response = $this->fastapi()->get(
            $this->base()."/api/v1/admin/target_recommendation/runs/{$run_id}/geojson",
        );
        if (! $response->ok()) {
            return response()->json(['error' => $response->body()], 502);
        }

        return response()->json($response->json());
    }

    public function signoff(Request $request, string $run_id): JsonResponse
    {
        $this->authorize('admin');
        if (! preg_match('/^[0-9a-f-]{36}$/i', $run_id)) {
            return response()->json(['error' => 'invalid run_id'], 400);
        }

        $payload = $request->validate([
            'target_id' => ['required', 'uuid'],
            'qp_user_id' => ['required', 'integer'],
            'qp_credential_id' => ['required', 'string', 'max:200'],
            'decision' => ['required', 'in:accepted,modified,rejected,signed_off'],
            'rationale' => ['required', 'string', 'min:1', 'max:5000'],
            'qp_signature_method' => ['nullable', 'string', 'max:50'],
            'credential_verified' => ['nullable', 'boolean'],
        ]);

        $response = $this->fastapi()->post(
            $this->base()."/api/v1/admin/target_recommendation/runs/{$run_id}/signoff",
            $payload,
        );

        if ($response->status() === 422) {
            return response()->json(
                ['error' => 'invariant violation', 'fastapi_body' => $response->json()],
                422,
            );
        }
        if (! $response->ok()) {
            return response()->json(
                ['error' => 'fastapi returned non-2xx', 'fastapi_status' => $response->status()],
                502,
            );
        }

        return response()->json($response->json());
    }

    private function fastapi()
    {
        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            abort(500, 'FASTAPI_SERVICE_KEY not configured');
        }

        return Http::withHeaders(['X-Service-Key' => $serviceKey])->timeout(30);
    }

    private function base(): string
    {
        return rtrim(
            config('services.fastapi.internal_url')
                ?? config('services.fastapi.internal_url'),
            '/',
        );
    }
}
