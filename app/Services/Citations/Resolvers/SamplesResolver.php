<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;

/**
 * Resolves `silver.samples:element=<element>:count=<n>` chunk ids — typed
 * citation pointing at an aggregated assay query.
 *
 * The id names an element and a count, not a row, so "exists" means: the
 * element is present in assay data inside the projects the request
 * authorises (silver.assays_v2.element, or a key of
 * silver.samples.commodity_assays). The old resolver echoed any element and
 * count back as fact. A `count=0` citation records a query that matched
 * nothing, so it cannot be checked against data and is answered with the
 * element only after it passes the shape check.
 */
final class SamplesResolver extends AbstractCitationResolver
{
    public static function prefix(): string
    {
        return 'silver.samples:';
    }

    /**
     * @param list<string>|null $projectIds
     */
    public function resolve(string $sourceId, ?string $workspaceId = null, ?array $projectIds = null): JsonResponse
    {
        if ($workspaceId === null || $projectIds === null || $projectIds === []) {
            return $this->notFound($sourceId);
        }

        // Element symbols and commodity_assays keys: U3O8, U3O8_ppm, Au_ppb,
        // Cu%, ... Anything else is not an element and is never echoed.
        if (! preg_match('/element=([A-Za-z0-9_.%+\-]{1,64})(?::|$)/', $sourceId, $elementMatch)) {
            return $this->notFound($sourceId);
        }
        $element = $elementMatch[1];

        preg_match('/count=(\d+)/', $sourceId, $countMatch);
        $count = isset($countMatch[1]) ? (int) $countMatch[1] : null;

        if ($count !== 0 && ! $this->elementExistsInScope($element, $workspaceId, $projectIds)) {
            return $this->notFound($sourceId);
        }

        return response()->json([
            'source_type' => 'samples',
            'source_chunk_id' => $sourceId,
            'title' => "Assay Data: {$element}",
            'text' => ($count ?? '?')." assay samples for element {$element}.",
            'metadata' => ['element' => $element, 'count' => $count !== null ? (string) $count : '?'],
        ]);
    }

    /**
     * @param list<string> $projectIds
     */
    private function elementExistsInScope(string $element, string $workspaceId, array $projectIds): bool
    {
        $inAssays = DB::table('silver.assays_v2 as a')
            ->join('silver.collars as c', 'c.collar_id', '=', 'a.collar_id')
            ->where('a.workspace_id', $workspaceId)
            ->whereIn('c.project_id', $projectIds)
            ->where('a.element', $element)
            ->exists();

        if ($inAssays) {
            return true;
        }

        $samples = DB::table('silver.samples as s')
            ->join('silver.collars as c', 'c.collar_id', '=', 's.collar_id')
            ->where('s.workspace_id', $workspaceId)
            ->whereIn('c.project_id', $projectIds);

        if (DB::connection()->getDriverName() === 'pgsql') {
            // jsonb_exists() is the `?` operator without the PDO placeholder clash.
            $samples->whereRaw('jsonb_exists(s.commodity_assays::jsonb, ?)', [$element]);
        } else {
            $samples->where('s.commodity_assays', 'like', '%"'.$element.'"%');
        }

        return $samples->exists();
    }

    /**
     * Structured 404 — identical for "missing" and "cross-tenant" so the
     * endpoint is not an existence oracle.
     */
    private function notFound(string $sourceId): JsonResponse
    {
        return response()->json([
            'source_type' => 'samples',
            'source_chunk_id' => $sourceId,
            'text' => 'Assay data not found',
        ], 404);
    }
}
