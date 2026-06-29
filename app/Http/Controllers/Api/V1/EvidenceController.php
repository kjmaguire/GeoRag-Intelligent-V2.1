<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Services\FastApiJwtMinter;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;

/**
 * GET /api/v1/evidence/{evidenceId} — Laravel proxy for FastAPI's
 * /v1/evidence/{evidence_id} endpoint.
 *
 * Background — 2026-06-03 audit pass 6 (Theme K)
 * -----------------------------------------------
 * The React `EvidenceInspector` previously called FastAPI directly
 * via `/fastapi/v1/evidence/{id}` and sent `X-Service-Key:
 * import.meta.env.VITE_SERVICE_KEY`. With VITE_SERVICE_KEY empty
 * (the documented state) the endpoint returned 401 every time —
 * Evidence Inspector was non-functional. If anyone HAD set
 * VITE_SERVICE_KEY for "convenience" Vite would have inlined the
 * service key into the production JS bundle — a textbook
 * secret-in-bundle leak.
 *
 * Resolution: route through Laravel. This controller:
 *   1. Confirms the caller is authenticated (Sanctum session).
 *   2. Resolves the evidence_item's project_id from DB and gates
 *      on `hasProjectAccess` (Theme H pattern).
 *   3. Mints a short-TTL FastAPI JWT with the verified project_id.
 *   4. Injects the server-side service key.
 *   5. Forwards GET to FastAPI and returns the JSON.
 *
 * Service key never leaves the server.
 */
class EvidenceController extends Controller
{
    public function show(Request $request, string $evidenceId): JsonResponse
    {
        $user = $request->user();
        if ($user === null) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        // UUID-ish guard up front so a hostile evidenceId can't be
        // used to probe for FastAPI internal state via injected URL
        // path segments. Loose pattern — evidence ids carry a couple
        // of shapes (raw UUID, prefixed sentinels like `evid:` etc.)
        // but the unsafe characters (/, ?, #, control bytes) are
        // never legitimate.
        if ($evidenceId === '' || preg_match('@[\\\\/\\?#\\x00-\\x1f]@', $evidenceId)) {
            return response()->json(['error' => 'invalid_evidence_id'], 400);
        }

        // Tenancy gate — resolve the evidence_item's project_id and
        // verify access. Same shape as TrustController +
        // PublicApiController::answer.
        $row = DB::table('silver.evidence_items')
            ->where('evidence_id', $evidenceId)
            ->select('project_id', 'workspace_id')
            ->first();
        if ($row === null
            || $row->project_id === null
            || ! $user->hasProjectAccess((string) $row->project_id)
        ) {
            return response()->json(['error' => 'not_found'], 404);
        }

        $serviceKey = config('services.fastapi.service_key')
            ?? env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            return response()->json(['error' => 'fastapi service key missing'], 500);
        }
        $fastApiBase = rtrim(
            config('services.fastapi.internal_url')
                ?? env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
            '/',
        );

        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $user->id,
            (string) $row->project_id,
            [],
        );

        try {
            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'X-Workspace-Id' => (string) $row->workspace_id,
                'Accept' => 'application/json',
            ])->timeout(10)->get(
                $fastApiBase.'/v1/evidence/'.rawurlencode($evidenceId),
            );
        } catch (\Throwable $exc) {
            return response()->json(
                ['error' => 'fastapi unreachable', 'reason' => $exc->getMessage()],
                502,
            );
        }

        if (! $resp->ok()) {
            return response()->json(
                ['error' => 'fastapi non-2xx', 'status' => $resp->status()],
                $resp->status(),
            );
        }

        return response()->json($resp->json());
    }
}
