<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

/**
 * Resolves `silver.collars:*` chunk ids to a description of the underlying
 * drill collar. FastAPI (`response_assembler.py`) emits three live forms:
 *
 *   - `silver.collars:count=N:first=<collar_id>` — spatial query. `count=N`
 *     summarises the retrieval batch (e.g. 20 collars) and `first=` pins the
 *     first row's UUID. We resolve the FIRST row and let the user navigate
 *     from there; the card is a representative anchor, not the full set.
 *   - `silver.collars:hole=<hole_id>:collar=<collar_id>:assays=N:litho=N` —
 *     single-collar details. `collar=` is the UUID to resolve.
 *   - `silver.collars:miss` — the collar lookup found nothing; there is
 *     nothing to resolve, so this is a 404.
 *
 * A bare `silver.collars:count=N` (no `first=`, no `collar=`) is an
 * empty-result summary and keeps its generic description.
 */
final class CollarsResolver extends AbstractCitationResolver
{
    public static function prefix(): string
    {
        return 'silver.collars:';
    }

    /**
     * @param list<string>|null $projectIds
     */
    public function resolve(string $sourceId, ?string $workspaceId = null, ?array $projectIds = null): JsonResponse
    {
        // `:miss` — FastAPI's "no such collar" marker. Nothing to resolve.
        if ($sourceId === 'silver.collars:miss') {
            return $this->notFound($sourceId);
        }

        // `first=` (spatial query) wins; fall back to `collar=` (single
        // collar details). Both must be a full UUID so a malformed id
        // cannot reach the uuid-typed column comparison.
        $collarId = null;
        if (preg_match('/first=([^:]+)/', $sourceId, $matches) === 1) {
            $collarId = $matches[1];
        } elseif (preg_match('/collar=([0-9a-f-]{36})/i', $sourceId, $matches) === 1) {
            $collarId = $matches[1];
        }

        if ($collarId === null) {
            return response()->json([
                'source_type' => 'collars',
                'text' => 'Collar data query result',
            ]);
        }

        // Belt and braces (security fix 2026-08-14): explicit tenant filter
        // on top of the controller-bound RLS GUC; null scope fails CLOSED.
        // `first=` is captured loosely, so the "must be a full UUID" rule
        // above is enforced here: a malformed id reaching the uuid column is
        // a 22P02 and a 500, where "not found" is the answer.
        if ($workspaceId === null || $projectIds === null || $projectIds === [] || ! Str::isUuid($collarId)) {
            return $this->notFound($sourceId);
        }

        $collar = DB::table('silver.collars')
            ->where('collar_id', $collarId)
            ->where('workspace_id', $workspaceId)
            ->whereIn('project_id', $projectIds)
            ->first(['collar_id', 'hole_id', 'total_depth', 'hole_type', 'status', 'drill_date']);

        if (! $collar) {
            return $this->notFound($sourceId);
        }

        return response()->json([
            'source_type' => 'collars',
            'source_chunk_id' => $sourceId,
            'title' => "Drill Collar: {$collar->hole_id}",
            'text' => sprintf(
                '%s — %s, %s, Status: %s, Drilled: %s',
                $collar->hole_id,
                $collar->hole_type,
                // Optional since 2026-09-29 (§04e): never "0.0 m TD".
                $collar->total_depth !== null
                    ? number_format((float) $collar->total_depth, 1).' m TD'
                    : 'TD not recorded',
                $collar->status,
                $collar->drill_date ?? 'unknown',
            ),
            'metadata' => (array) $collar,
        ]);
    }

    /**
     * Structured 404 — identical for "missing" and "cross-tenant" so the
     * endpoint is not an existence oracle.
     */
    private function notFound(string $sourceId): JsonResponse
    {
        return response()->json([
            'source_type' => 'collars',
            'source_chunk_id' => $sourceId,
            'text' => 'Collar not found',
        ], 404);
    }
}
