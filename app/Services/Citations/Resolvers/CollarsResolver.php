<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;

/**
 * Resolves `silver.collars:count=N:first=<collar_id>` chunk ids to a
 * description of the underlying drill collar.
 *
 * Citation format note: the dispatcher emits a `count=N` that summarises
 * the original retrieval batch (e.g. 20 collars), and `first=<collar_id>`
 * pinning the first row's UUID. We resolve the FIRST row and let the user
 * navigate from there if they want the full set — citation cards represent
 * a representative anchor, not the complete result set.
 */
final class CollarsResolver extends AbstractCitationResolver
{
    public static function prefix(): string
    {
        return 'silver.collars:';
    }

    public function resolve(string $sourceId): JsonResponse
    {
        preg_match('/first=([^:]+)/', $sourceId, $matches);
        $collarId = $matches[1] ?? null;

        if (! $collarId) {
            return response()->json([
                'source_type' => 'collars',
                'text'        => 'Collar data query result',
            ]);
        }

        $collar = DB::table('silver.collars')
            ->where('collar_id', $collarId)
            ->first(['collar_id', 'hole_id', 'total_depth', 'hole_type', 'status', 'drill_date']);

        if (! $collar) {
            return response()->json([
                'source_type' => 'collars',
                'text'        => 'Collar not found',
            ]);
        }

        return response()->json([
            'source_type'     => 'collars',
            'source_chunk_id' => $sourceId,
            'title'           => "Drill Collar: {$collar->hole_id}",
            'text'            => sprintf(
                '%s — %s, %s m TD, Status: %s, Drilled: %s',
                $collar->hole_id,
                $collar->hole_type,
                number_format((float) $collar->total_depth, 1),
                $collar->status,
                $collar->drill_date ?? 'unknown',
            ),
            'metadata' => (array) $collar,
        ]);
    }
}
