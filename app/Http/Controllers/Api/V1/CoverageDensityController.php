<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Services\FastApiJwtMinter;
use App\Support\AuthorizationAuditLogger;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;

/**
 * CC-03 Item 5 — coverage density GeoJSON proxy.
 *
 * Thin Laravel bridge in front of FastAPI's GET /coverage/density. Lets the
 * MapView render the layer through the Sanctum-authenticated session cookie
 * without exposing the service key to the browser. Validates the workspace
 * via project membership before minting the service JWT.
 *
 *   GET /api/v1/projects/{projectId}/coverage-density
 *       ?kind=collars|reports|spatial_features
 *       &cell_size_m=500|1000|5000|10000
 */
class CoverageDensityController extends Controller
{
    public function show(Request $request, string $projectId): JsonResponse
    {
        if (! $request->user()->hasProjectAccess($projectId)) {
            AuthorizationAuditLogger::deny(
                actor: $request->user(),
                targetResource: "project:{$projectId}",
                reason: 'no_pivot_row',
                context: ['action' => __FUNCTION__, 'path' => $request->path()],
            );

            return response()->json(['message' => 'Project not found.'], 404);
        }

        $validated = $request->validate([
            'kind' => 'sometimes|in:collars,reports,spatial_features',
            'cell_size_m' => 'sometimes|integer|in:500,1000,5000,10000',
        ]);
        $kind = $validated['kind'] ?? 'collars';
        $cellSizeM = (int) ($validated['cell_size_m'] ?? 1000);

        $workspaceId = (string) DB::table('silver.projects')
            ->where('project_id', $projectId)
            ->value('workspace_id');
        if ($workspaceId === '') {
            return response()->json(['message' => 'Project not found.'], 404);
        }

        $fastApiBase = rtrim(
            (string) (config('services.fastapi.internal_url')
                ?? config('services.fastapi.internal_url')),
            '/',
        );
        $serviceKey = config('services.fastapi.service_key') ?? config('services.fastapi.service_key');
        if (! $serviceKey) {
            return response()->json(['message' => 'FastAPI service key not configured.'], 503);
        }

        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $request->user()->id,
            $projectId,
            roles: [],
            workspaceId: $workspaceId,
        );

        $resp = Http::withHeaders([
            'X-Service-Key' => $serviceKey,
            'Authorization' => 'Bearer '.$jwt,
            'Accept' => 'application/json',
        ])->timeout(30)->get($fastApiBase.'/coverage/density', [
            'project_id' => $projectId,
            'kind' => $kind,
            'cell_size_m' => $cellSizeM,
        ]);

        if (! $resp->ok()) {
            return response()->json(
                [
                    'message' => 'FastAPI coverage density returned HTTP '.$resp->status(),
                    'detail' => $resp->json(),
                ],
                502,
            );
        }

        return response()->json($resp->json());
    }
}
