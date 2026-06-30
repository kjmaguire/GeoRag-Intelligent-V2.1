<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;

/**
 * Resolves `silver.lithology_logs:hole=<hole_id>:collar=<uuid>:intervals=N`
 * chunk ids. Currently returns a summary stub — the underlying interval
 * detail is rendered by the strip-log component, not the citation viewer.
 */
final class LithologyResolver extends AbstractCitationResolver
{
    public static function prefix(): string
    {
        return 'silver.lithology_logs:';
    }

    public function resolve(string $sourceId): JsonResponse
    {
        preg_match('/hole=([^:]+)/', $sourceId, $matches);
        $holeId = $matches[1] ?? 'unknown';

        return response()->json([
            'source_type' => 'lithology',
            'source_chunk_id' => $sourceId,
            'title' => "Lithology Log: {$holeId}",
            'text' => "Lithology interval data for drill hole {$holeId}.",
            'metadata' => ['hole_id' => $holeId],
        ]);
    }
}
