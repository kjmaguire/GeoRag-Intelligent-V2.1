<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Services\FastApiJwtMinter;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;
use Symfony\Component\HttpKernel\Exception\NotFoundHttpException;

/**
 * §19.3 Interpretation Workspace — Inertia page + thin proxy.
 *
 * Routes:
 *   GET    /projects/{projectId}/interpretation                  — Inertia page
 *   GET    /api/v1/interpretation/notes?project_id=…             — proxy
 *   POST   /api/v1/interpretation/notes
 *   DELETE /api/v1/interpretation/notes/{noteId}
 *   GET    /api/v1/interpretation/section-lines?project_id=…
 *   POST   /api/v1/interpretation/section-lines
 *   DELETE /api/v1/interpretation/section-lines/{sectionId}
 *   GET    /api/v1/interpretation/target-zones?project_id=…
 *   POST   /api/v1/interpretation/target-zones
 *   POST   /api/v1/interpretation/target-zones/{zoneId}/accept
 *   DELETE /api/v1/interpretation/target-zones/{zoneId}
 *
 * Auth: Sanctum. The proxy mints a short-TTL JWT carrying the user's id
 * and the active project so FastAPI can RLS-scope writes correctly.
 */
class InterpretationWorkspaceController extends Controller
{
    public function index(Request $request, string $projectId): Response
    {
        // Tenancy gate + real workspace resolution (2026-06-03 audit).
        //
        // Previously hard-coded workspace_id to the default-tenant placeholder
        // — every user (regardless of project / workspace) saw the same value.
        // The downstream React page then used that hardcoded id for any
        // workspace-scoped Reverb subscriptions / API calls, so a non-default
        // tenant's interpretation activity was emitted on a foreign channel.
        //
        // Resolve the real workspace_id from silver.projects and gate
        // on hasProjectAccess so the page can't be loaded for a project
        // the caller doesn't own.
        $user = $request->user();
        if ($user === null || ! $user->hasProjectAccess($projectId)) {
            throw new NotFoundHttpException;
        }
        $workspaceId = DB::table('silver.projects')
            ->where('project_id', $projectId)
            ->value('workspace_id');
        if ($workspaceId === null) {
            throw new NotFoundHttpException;
        }

        return Inertia::render('InterpretationWorkspace', [
            'project_id' => $projectId,
            'workspace_id' => (string) $workspaceId,
        ]);
    }

    public function proxy(Request $request, string $tail): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $fastApiBase = rtrim(
            config('services.fastapi.internal_url')
                ?? config('services.fastapi.internal_url'),
            '/',
        );
        $serviceKey = config('services.fastapi.service_key')
            ?? config('services.fastapi.service_key');
        if (! $serviceKey) {
            return response()->json(['error' => 'fastapi service key missing'], 500);
        }

        // Tenancy gate (Theme H extension — 2026-06-03 audit pass 5+++)
        //
        // Same shape as the TrustController bug — minting a JWT with a
        // caller-supplied project_id lets an attacker in workspace A
        // trigger interpretation reads/writes for any project they can
        // name. The FastAPI side trusts the JWT's project claim for
        // workspace resolution, so the gate must be here.
        $projectId = (string) $request->query('project_id', '');
        if ($projectId === '' || ! $user->hasProjectAccess($projectId)) {
            return response()->json(['error' => 'not_found'], 404);
        }
        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $user->id,
            $projectId,
            [],
        );

        $url = $fastApiBase.'/v1/interpretation/'.ltrim($tail, '/');
        $headers = [
            'X-Service-Key' => $serviceKey,
            'Authorization' => 'Bearer '.$jwt,
            'Accept' => 'application/json',
        ];

        $method = strtoupper($request->method());
        $client = Http::withHeaders($headers)->timeout(15);

        try {
            $resp = match ($method) {
                'GET' => $client->get($url, $request->query()),
                'POST' => $client->post($url, $request->all()),
                'PUT' => $client->put($url, $request->all()),
                'DELETE' => $client->delete($url),
                default => abort(405),
            };
        } catch (\Throwable $exc) {
            return response()->json([
                'error' => 'fastapi unreachable',
                'reason' => $exc->getMessage(),
            ], 502);
        }

        if (! $resp->ok() && $resp->status() !== 204) {
            return response()->json([
                'error' => 'fastapi non-2xx',
                'status' => $resp->status(),
                'body' => $resp->json() ?? $resp->body(),
            ], $resp->status());
        }

        // 204 No Content — return empty json with 204
        if ($resp->status() === 204) {
            return response()->json(null, 204);
        }

        return response()->json($resp->json());
    }
}
