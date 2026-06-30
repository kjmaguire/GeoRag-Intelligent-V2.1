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
 * /admin/ml/training-runs — list + trigger for both training workflows.
 *
 * Backed by FastAPI's `/api/v1/admin/ml/*` router. Auth: 'admin' Gate.
 */
class MlTrainingRunsController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $response = $this->fastapi()->get(
            $this->base().'/api/v1/admin/ml/training-runs',
            ['limit' => 100],
        );

        return Inertia::render('Admin/MlTrainingRuns', [
            'runs' => $response->ok() ? ($response->json('runs') ?? []) : [],
            'fastapi_error' => $response->ok() ? null : $response->body(),
        ]);
    }

    public function trainTargetModel(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $payload = $request->validate([
            'target_model_id' => ['required', 'uuid'],
            'initiated_by_user_id' => ['required', 'integer'],
            'activate_on_success' => ['nullable', 'boolean'],
            'min_outcomes_per_deposit_model' => ['nullable', 'integer', 'min:1', 'max:1000'],
        ]);

        $response = $this->fastapi()->post(
            $this->base().'/api/v1/admin/ml/train-target-model', $payload,
        );

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

    public function trainSourceTrust(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'initiated_by_user_id' => ['required', 'integer'],
            'min_citations_per_source' => ['nullable', 'integer', 'min:1', 'max:1000'],
            'model_version' => ['nullable', 'string', 'max:40'],
        ]);

        $response = $this->fastapi()->post(
            $this->base().'/api/v1/admin/ml/train-source-trust', $payload,
        );

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

    private function fastapi()
    {
        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            abort(500, 'FASTAPI_SERVICE_KEY not configured');
        }

        return Http::withHeaders(['X-Service-Key' => $serviceKey])->timeout(120);
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
