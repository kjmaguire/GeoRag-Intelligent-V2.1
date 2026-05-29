<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Services\Citations\CitationResolverRegistry;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

/**
 * Citation source lookup — resolves a `source_chunk_id` to the underlying
 * source text, section, and provenance metadata.
 *
 * Used by the Document Viewer to display the exact source content that a
 * citation refers to, enabling QP-level verification of RAG answers.
 *
 * Routes:
 *   GET /api/v1/citations/resolve?source_chunk_id=...&citation_type=...
 *
 * Architecture
 * ------------
 * The controller is intentionally thin — it delegates dispatch to
 * `CitationResolverRegistry`, which maps each `source_chunk_id` prefix to a
 * dedicated `CitationResolver` implementation. This refactor (2026-05-07)
 * replaced an 11-branch `if (str_starts_with(...))` chain with a strategy
 * pattern; adding a new source type is now:
 *
 *   1. Add a new class in `app/Services/Citations/Resolvers/`.
 *   2. Register it in `App\Providers\CitationResolverServiceProvider`.
 *
 * No edit to this controller. No edit to the dispatcher.
 *
 * Supported source_chunk_id prefixes
 * ----------------------------------
 *   silver.collars:count=20:first=...
 *   silver.lithology_logs:hole=PLS-20-01:collar=...:intervals=4
 *   silver.samples:element=U3O8_ppm:count=25
 *   georag_reports:44a67709-...:section=13:chunk=...
 *   pg_mine:CA-SK-MINE-LOC:feature=12345:pg_id=<uuid>
 *   pg_mineral_occurrence:CA-SK-SMDI:feature=7788:pg_id=<uuid>
 *   pg_drillhole_collar:CA-SK-DRILLHOLE:feature=9001:pg_id=<uuid>
 *   pg_resource_potential_zone:CA-SK-RESOURCE-POTENTIAL-GOLD:feature=...:pg_id=<uuid>
 *   pg_rock_sample:CA-SK-ROCK-SAMPLE:feature=...:pg_id=<uuid>
 *   pg_assessment_survey:CA-SK-SMAD:feature=...:pg_id=<uuid>
 *   pg_mineral_disposition:CA-SK-MINERAL-DISPOSITION:feature=...:pg_id=<uuid>
 */
final class CitationController extends Controller
{
    public function __construct(
        private readonly CitationResolverRegistry $registry,
    ) {
    }

    /**
     * Resolve a source_chunk_id to its original content.
     *
     * Returns 200 in every successful path — including "not recognised" and
     * "record not found" cases. The citation viewer surfaces the gap to the
     * user; an HTTP 404 would be invisible.
     *
     * Returns 400 only when the required query parameter is missing.
     */
    public function resolve(Request $request): JsonResponse
    {
        $sourceId = (string) $request->query('source_chunk_id', '');

        if ($sourceId === '') {
            return response()->json(
                ['message' => 'source_chunk_id is required'],
                400,
            );
        }

        $resolved = $this->registry->resolve($sourceId);

        if ($resolved !== null) {
            return $resolved;
        }

        // Unknown prefix — return a structured "not recognised" payload so
        // the citation viewer can render a helpful empty state.
        return response()->json([
            'source_type'     => 'unknown',
            'source_chunk_id' => $sourceId,
            'text'            => 'Source type not recognized.',
            'metadata'        => [],
        ]);
    }
}
