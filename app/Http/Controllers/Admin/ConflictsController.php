<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Inertia\Inertia;
use Inertia\Response;

/**
 * §7.4 Conflict Resolver review queue + test-bench (Phase H4 UI).
 *
 *   GET  /admin/conflicts            — recent audit entries + test bench
 *   POST /admin/conflicts/run        — run the resolver on caller-supplied claims
 */
class ConflictsController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $response = $this->fastapi()->get(
            $this->base().'/api/v1/admin/conflicts/recent',
            ['limit' => 100],
        );

        return Inertia::render('Admin/Conflicts', [
            'entries' => $response->ok() ? ($response->json('entries') ?? []) : [],
            'fastapi_error' => $response->ok() ? null : $response->body(),
        ]);
    }

    public function run(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'section_id' => ['nullable', 'string', 'max:60'],
            'claims' => ['required', 'array', 'min:1'],
            'claims.*.claim_id' => ['required', 'string'],
            'claims.*.text' => ['required', 'string'],
            'claims.*.validated' => ['nullable', 'boolean'],
            'claims.*.evidence' => ['nullable', 'array'],
            'workspace_data_version' => ['nullable', 'integer'],
        ]);

        $response = $this->fastapi()->post(
            $this->base().'/api/v1/admin/conflicts/run', $payload,
        );
        if (! $response->ok()) {
            return response()->json(
                ['error' => 'fastapi error', 'fastapi_body' => $response->json()],
                502,
            );
        }

        // Phase 5 — broadcast Admin/Conflicts refresh on successful run.
        // Best-effort; broadcast failure must not fail the API response.
        try {
            AdminSurfaceUpdated::dispatch(
                'conflicts',
                null,
                ['entries'],
                [
                    'workspace_id' => $payload['workspace_id'],
                    'section_id' => $payload['section_id'] ?? null,
                    'claim_count' => count($payload['claims']),
                ],
            );
        } catch (\Throwable $e) {
            Log::warning(
                'ConflictsController: surface broadcast failed',
                ['error' => $e->getMessage()],
            );
        }

        return response()->json($response->json());
    }

    private function fastapi()
    {
        $key = env('FASTAPI_SERVICE_KEY');
        if (! $key) {
            abort(500, 'FASTAPI_SERVICE_KEY not configured');
        }

        return Http::withHeaders(['X-Service-Key' => $key])->timeout(30);
    }

    private function base(): string
    {
        return rtrim(
            config('services.fastapi.internal_url')
                ?? env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
            '/',
        );
    }
}
