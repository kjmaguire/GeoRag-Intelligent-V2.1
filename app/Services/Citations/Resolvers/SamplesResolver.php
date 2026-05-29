<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;

/**
 * Resolves `silver.samples:element=<element>:count=<n>` chunk ids — typed
 * citation pointing at an aggregated assay query.
 */
final class SamplesResolver extends AbstractCitationResolver
{
    public static function prefix(): string
    {
        return 'silver.samples:';
    }

    public function resolve(string $sourceId): JsonResponse
    {
        preg_match('/element=([^:]+)/', $sourceId, $matches);
        $element = $matches[1] ?? 'unknown';

        preg_match('/count=(\d+)/', $sourceId, $countMatch);
        $count = $countMatch[1] ?? '?';

        return response()->json([
            'source_type'     => 'samples',
            'source_chunk_id' => $sourceId,
            'title'           => "Assay Data: {$element}",
            'text'            => "{$count} assay samples for element {$element}.",
            'metadata'        => ['element' => $element, 'count' => $count],
        ]);
    }
}
