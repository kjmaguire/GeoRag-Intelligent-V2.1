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
     * Returns a 200 response on success (even if the underlying record is
     * not found — the response body explains the gap rather than 404'ing,
     * because the caller is the citation viewer and an invisible 404 is
     * worse UX than a "not found" message).
     */
    public function resolve(string $sourceId): JsonResponse;
}
