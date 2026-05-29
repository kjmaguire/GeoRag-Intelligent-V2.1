<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use App\Services\Citations\Contracts\CitationResolver;

/**
 * Shared helpers used across multiple resolver implementations.
 *
 * Provides:
 *   - `parsePgArray()` — coerces a PostgreSQL TEXT[] value (returned either
 *     as a PHP array by pdo_pgsql or as a `{a,b,c}` literal string by some
 *     drivers) into a clean PHP array.
 *   - `decodeSignals()` — accepts a JSON-encoded or already-decoded signals
 *     payload and returns it as a normalised array.
 *
 * These were `private` methods on the original `CitationController` and
 * lived alongside the resolver dispatcher. Extracted here so concrete
 * resolvers can share them via inheritance instead of duplicating.
 */
abstract class AbstractCitationResolver implements CitationResolver
{
    /**
     * Coerce a PostgreSQL TEXT[] into a clean array of non-empty strings.
     *
     * Accepts:
     *   - `null` → []
     *   - PHP array (pdo_pgsql) → array_values(non-empty strings)
     *   - `'{a,b,c}'` literal → exploded + trimmed of surrounding quotes
     *   - `'a,b,c'` plain CSV (some drivers) → exploded + trimmed
     *
     * @return list<string>
     */
    protected function parsePgArray(mixed $value): array
    {
        if ($value === null) {
            return [];
        }
        if (is_array($value)) {
            return array_values(array_filter(array_map('strval', $value), fn ($v) => $v !== ''));
        }
        $s = trim((string) $value);
        if ($s === '' || $s === '{}') {
            return [];
        }
        if (str_starts_with($s, '{') && str_ends_with($s, '}')) {
            $inner = substr($s, 1, -1);
            $parts = array_map(
                fn ($p) => trim(trim($p), '"'),
                explode(',', $inner),
            );
            return array_values(array_filter($parts, fn ($v) => $v !== ''));
        }
        return array_values(array_filter(array_map('trim', explode(',', $s))));
    }

    /**
     * Coerce a JSON-encoded or already-decoded signals payload into an
     * array. Returns [] for any value that doesn't decode to an array.
     *
     * @return array<mixed>
     */
    protected function decodeSignals(mixed $value): array
    {
        if ($value === null) {
            return [];
        }
        if (is_array($value)) {
            return $value;
        }
        $decoded = json_decode((string) $value, true);
        return is_array($decoded) ? $decoded : [];
    }
}
