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
 * §7 Report Builder Cockpit (Phase H4 UI).
 *
 *   GET  /admin/reports             — picker + recent builds
 *   POST /admin/reports/build       — plan a new build
 *   GET  /admin/reports/{build_id}  — single build progress
 */
class ReportBuilderController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $typesResp = $this->fastapi()->get(
            $this->base().'/api/v1/admin/reports/types',
        );
        $buildsResp = $this->fastapi()->get(
            $this->base().'/api/v1/admin/reports/builds',
            ['limit' => 50],
        );

        return Inertia::render('Admin/ReportBuilder', [
            'manifest' => $typesResp->ok() ? $typesResp->json() : null,
            'builds' => $buildsResp->ok() ? ($buildsResp->json('builds') ?? []) : [],
            'fastapi_error' => $typesResp->ok() ? null : $typesResp->body(),
        ]);
    }

    public function build(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $payload = $request->validate([
            'report_type' => ['required', 'string', 'max:60'],
            'workspace_id' => ['required', 'uuid'],
            'project_id' => ['required', 'uuid'],
            'requested_by_user_id' => ['required', 'integer'],
        ]);

        $response = $this->fastapi()->post(
            $this->base().'/api/v1/admin/reports/build', $payload,
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

    public function export(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'project_id' => ['required', 'uuid'],
            'report_type' => ['required', 'string', 'max:60'],
            'requested_by_user_id' => ['required', 'integer'],
            'report_window_start_iso' => ['nullable', 'string'],
            'report_window_end_iso' => ['nullable', 'string'],
            'delivery_targets' => ['nullable', 'array'],
        ]);

        $response = $this->fastapi()->timeout(300)->post(
            $this->base().'/api/v1/admin/reports/export', $payload,
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

    public function sectionHistory(Request $request, string $build_id, string $section_id): JsonResponse
    {
        $this->authorize('admin');
        if (! preg_match('/^[0-9a-f-]{36}$/i', $build_id)) {
            return response()->json(['error' => 'invalid build_id'], 400);
        }
        $limit = max(1, min(200, (int) $request->query('limit', 50)));

        $response = $this->fastapi()->get(
            $this->base()."/api/v1/admin/reports/builds/{$build_id}/sections/".rawurlencode($section_id).'/history',
            ['limit' => $limit],
        );
        if ($response->status() === 404) {
            return response()->json(['error' => 'build not found'], 404);
        }
        if (! $response->ok()) {
            return response()->json(
                ['error' => 'fastapi returned non-2xx',
                    'fastapi_status' => $response->status(),
                    'fastapi_body' => $response->json()],
                502,
            );
        }

        return response()->json($response->json());
    }

    public function saveSection(Request $request, string $build_id, string $section_id): JsonResponse
    {
        $this->authorize('admin');
        if (! preg_match('/^[0-9a-f-]{36}$/i', $build_id)) {
            return response()->json(['error' => 'invalid build_id'], 400);
        }

        $payload = $request->validate([
            'body_markdown' => ['required', 'string', 'max:200000'],
            'updated_by_user_id' => ['required', 'integer'],
        ]);

        $response = $this->fastapi()->put(
            $this->base()."/api/v1/admin/reports/builds/{$build_id}/sections/".rawurlencode($section_id),
            $payload,
        );

        if (! $response->ok()) {
            return response()->json(
                ['error' => 'fastapi returned non-2xx',
                    'fastapi_status' => $response->status(),
                    'fastapi_body' => $response->json()],
                502,
            );
        }

        return response()->json($response->json());
    }

    public function show(Request $request, string $build_id): Response
    {
        $this->authorize('admin');
        if (! preg_match('/^[0-9a-f-]{36}$/i', $build_id)) {
            abort(404, 'invalid build_id');
        }

        $response = $this->fastapi()->get(
            $this->base()."/api/v1/admin/reports/builds/{$build_id}",
        );
        if ($response->status() === 404) {
            abort(404, 'build not found');
        }
        if (! $response->ok()) {
            abort(502, 'fastapi error: '.$response->body());
        }

        return Inertia::render('Admin/ReportBuild', [
            'build' => $response->json(),
        ]);
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
