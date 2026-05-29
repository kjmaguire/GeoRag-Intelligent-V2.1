<?php

declare(strict_types=1);

namespace App\Services\Citations;

use App\Services\Citations\Contracts\CitationResolver;
use Illuminate\Http\JsonResponse;

/**
 * Maps `source_chunk_id` prefixes to resolver instances.
 *
 * Replaces the original 11-branch `if (str_starts_with(...))` chain in
 * CitationController. Adding a new source type is now a two-step process:
 *   1. Implement `CitationResolver` in `app/Services/Citations/Resolvers/`.
 *   2. Register it in `App\Providers\CitationResolverServiceProvider`.
 *
 * No edit to the controller, no edit to the dispatcher.
 *
 * Octane safety
 * -------------
 * The registry is constructed once at app boot in the service provider's
 * `register()` method and stored as a singleton. Resolvers are stateless —
 * each `resolve()` call uses request-scoped facades (DB::, etc.), so the
 * registry holds no per-request state across requests.
 */
final class CitationResolverRegistry
{
    /**
     * Resolvers indexed by their declared prefix string.
     *
     * @var array<string, CitationResolver>
     */
    private array $resolvers = [];

    /**
     * Register a resolver. Subsequent registrations against the same prefix
     * overwrite the previous binding (useful in tests; harmless in prod
     * because each prefix is owned by exactly one resolver).
     */
    public function register(CitationResolver $resolver): void
    {
        $this->resolvers[$resolver::prefix()] = $resolver;
    }

    /**
     * Resolve a single `source_chunk_id` to a response, or null when no
     * registered resolver claims the prefix. The controller's fallback
     * handler converts null into the structured `unknown` payload.
     *
     * Order of evaluation: registration order. Each resolver's prefix is
     * conventionally globally unique within the GeoRAG corpus, so the
     * first-match rule is also the only-match rule in practice.
     */
    public function resolve(string $sourceId): ?JsonResponse
    {
        foreach ($this->resolvers as $prefix => $resolver) {
            if (str_starts_with($sourceId, $prefix)) {
                return $resolver->resolve($sourceId);
            }
        }
        return null;
    }

    /**
     * @return array<int, string>  Prefixes currently registered, for debug /
     *                             health-check / admin-route enumeration.
     */
    public function registeredPrefixes(): array
    {
        return array_keys($this->resolvers);
    }
}
