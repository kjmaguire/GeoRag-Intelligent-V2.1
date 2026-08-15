<?php

declare(strict_types=1);

namespace App\Services\Citations\Contracts;

use Illuminate\Http\JsonResponse;

/**
 * A citation resolver knows how to resolve a single `source_chunk_id` prefix
 * to a structured payload describing the underlying record.
 *
 * The dispatcher (`CitationResolverRegistry`) selects a resolver by matching
 * the inbound `source_chunk_id` against each registered resolver's
 * `prefix()`. The first match wins — prefixes are conventionally globally
 * unique within the GeoRAG corpus (see CitationController docstring for the
 * full prefix catalogue).
 *
 * Implementations
 * ---------------
 *   - Each concrete resolver targets exactly one prefix.
 *   - Resolvers are stateless and singleton-safe under Octane.
 *   - The base class `AbstractCitationResolver` provides shared helpers
 *     (PG array parsing, signal decoding); PGEO-specific resolvers extend
 *     `AbstractPgeoResolver` for the shared envelope + reference-summary
 *     plumbing.
 */
interface CitationResolver
{
    /**
     * The `source_chunk_id` prefix this resolver claims (e.g. `silver.collars:`,
     * `pg_mine:`, `georag_reports:`).
     *
     * Static so the registry can index resolvers without instantiating them
     * before dispatch.
     */
    public static function prefix(): string;

    /**
     * Resolve a single `source_chunk_id` to a JSON envelope describing the
     * underlying record.
     *
     * $workspaceId is the tenant scope the caller (CitationController) has
     * verified the authenticated user may read. Resolvers that query
     * tenant-scoped tables (silver.reports, silver.collars,
     * silver.assays_v2, …) MUST filter on it explicitly and MUST fail
     * CLOSED (miss) when it is null — never rely on the fail-open NULL-GUC
     * RLS fallback. Resolvers over workspace-global data (public
     * geoscience open-data tables) may ignore it.
     *
     * Returns 200 for a resolved record. Returns 404 (with a structured
     * body the citation viewer can still render) when the record is not
     * visible in the given workspace — deliberately identical for
     * "does not exist" and "exists in another tenant" so the endpoint is
     * not an existence oracle.
     */
    public function resolve(string $sourceId, ?string $workspaceId = null): JsonResponse;
}
