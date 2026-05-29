<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Services\FastApiJwtMinter;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * §17.3 Charts Gallery — Inertia page + proxy to /v1/viz/chart endpoint.
 *
 * Routes:
 *   GET  /charts/gallery                  — Inertia page (lists + previews all 8 kinds)
 *   POST /api/v1/charts/render            — proxy to POST /v1/viz/chart
 *   GET  /api/v1/charts/kinds             — proxy to GET /v1/viz/chart-kinds
 */
class ChartsGalleryController extends Controller
{
    private const KNOWN_CHARTS = [
        'long_section',
        'harker_diagram',
        'spider_diagram',
        'ree_pattern',
        'ternary_diagram',
        'grade_tonnage',
        'anomaly_map',
        'target_heatmap',
    ];

    public function gallery(Request $request): Response
    {
        return Inertia::render('ChartsGallery', [
            'chart_kinds' => self::KNOWN_CHARTS,
        ]);
    }

    public function render(Request $request): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $body = $request->validate([
            'chart_kind' => ['required', 'in:'.implode(',', self::KNOWN_CHARTS)],
            'params' => ['nullable', 'array'],
            'project_id' => ['nullable', 'uuid'],
            'commodity' => ['nullable', 'string', 'max:64'],
            'reference_azimuth_deg' => ['nullable', 'numeric'],
        ]);

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

        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $user->id,
            (string) $request->query('project_id', ''),
            [],
        );

        try {
            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(20)->post($fastApiBase.'/v1/viz/chart', $body);
        } catch (\Throwable $exc) {
            return response()->json(['error' => 'fastapi unreachable', 'reason' => $exc->getMessage()], 502);
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
