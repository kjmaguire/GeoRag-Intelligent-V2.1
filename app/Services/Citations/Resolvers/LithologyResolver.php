<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;

/**
 * Resolves `silver.lithology_logs:hole=<hole_id>:collar=<uuid>:intervals=N`
 * chunk ids. Currently returns a summary stub — the underlying interval
 * detail is rendered by the strip-log component, not the citation viewer.
 *
 * The stub used to be built from whatever the caller put in `hole=`, so any
 * authenticated user got an authoritative-looking "Lithology Log: <anything>"
 * for a hole that need not exist and an id that need not be theirs. It now
 * checks that the pinned collar exists inside the projects the request
 * authorises and takes the hole name from that row, never from the id.
 *
 * An id with no `collar=` (the orchestrator emits that form when no hole was
 * resolved) pins nothing, so it gets a generic payload that echoes no input,
 * exactly as the collar and assay resolvers answer for an unpinned id.
 */
final class LithologyResolver extends AbstractCitationResolver
{
    public static function prefix(): string
    {
        return 'silver.lithology_logs:';
    }

    /**
     * @param list<string>|null $projectIds
     */
    public function resolve(string $sourceId, ?string $workspaceId = null, ?array $projectIds = null): JsonResponse
    {
        if ($workspaceId === null || $projectIds === null || $projectIds === []) {
            return $this->notFound($sourceId);
        }

        if (! preg_match('/collar=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i', $sourceId, $matches)) {
            return response()->json([
                'source_type' => 'lithology',
                'source_chunk_id' => $sourceId,
                'text' => 'Lithology query result (no specific drill hole pinned)',
                'metadata' => [],
            ]);
        }

        $collar = DB::table('silver.collars')
            ->where('collar_id', $matches[1])
            ->where('workspace_id', $workspaceId)
            ->whereIn('project_id', $projectIds)
            ->first(['collar_id', 'hole_id']);

        if ($collar === null) {
            return $this->notFound($sourceId);
        }

        return response()->json([
            'source_type' => 'lithology',
            'source_chunk_id' => $sourceId,
            'title' => "Lithology Log: {$collar->hole_id}",
            'text' => "Lithology interval data for drill hole {$collar->hole_id}.",
            'metadata' => ['hole_id' => $collar->hole_id, 'collar_id' => $collar->collar_id],
        ]);
    }

    /**
     * Structured 404 — identical for "missing" and "cross-tenant" so the
     * endpoint is not an existence oracle.
     */
    private function notFound(string $sourceId): JsonResponse
    {
        return response()->json([
            'source_type' => 'lithology',
            'source_chunk_id' => $sourceId,
            'text' => 'Lithology log not found',
        ], 404);
    }
}
