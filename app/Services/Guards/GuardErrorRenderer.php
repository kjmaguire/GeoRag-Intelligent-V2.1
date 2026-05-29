<?php

declare(strict_types=1);

namespace App\Services\Guards;

/**
 * Resolves plan §4b GuardErrorCode values into user-facing strings.
 *
 * Reads templates from `lang/en/guard_errors.php` via Laravel's `__()`
 * translation helper. Handles the two degradation variants documented in
 * `docs/architecture/user_facing_error_catalog.md`:
 *
 *   ENTITY_NOT_FOUND → ENTITY_NOT_FOUND_NO_ALIASES
 *     When the caller can't supply a `suggested_aliases` value
 *     (typically because `silver.entity_aliases` is empty for this
 *     workspace), the renderer picks the no-aliases variant so the
 *     user doesn't see "Did you mean: ?".
 *
 *   CONFLICTING_SOURCES → CONFLICTING_SOURCES_WITH_AUTHORITY
 *     When plan §1h supersession has flagged one source as
 *     authoritative (`authoritative_doc` placeholder provided), the
 *     renderer picks the with-authority variant so the user sees
 *     "The current source is [doc]".
 *
 * Consumers:
 *   - `app/Http/Controllers/Api/V1/QueryController` — when FastAPI
 *     returns a `GeoRAGResponse` with non-empty `guard_error_codes`,
 *     render each one and attach to the response payload sent to the
 *     React client.
 *   - `app/Http/Middleware/HandleInertiaRequests` — share the full
 *     `guard_errors` translation map on every Inertia response so the
 *     React side can also render client-side (e.g. for cached
 *     responses or optimistic UI).
 *
 * Octane safety: stateless. Each call uses the request-scoped `__()`
 * facade. The class can be a container singleton without retaining
 * per-request state.
 */
final class GuardErrorRenderer
{
    /**
     * The full set of canonical plan §4b GuardErrorCode strings, plus
     * the degradation variants the renderer dispatches to.
     *
     * Used by `isKnownCode()` so callers can validate inputs before
     * persisting them anywhere (e.g. when the FastAPI payload carries
     * a code we don't recognise, we want to log + fall back rather
     * than render with a stale key).
     *
     * @var array<int, string>
     */
    private const KNOWN_CODES = [
        // Retrieval-failure codes (9)
        'NO_EVIDENCE_FOUND',
        'ENTITY_NOT_FOUND',
        'AMBIGUOUS_HOLE_ID',
        'AMBIGUOUS_FORMATION_NAME',
        'AMBIGUOUS_PROPERTY_NAME',
        'OVER_FILTERED_QUERY',
        'SPATIAL_QUERY_EMPTY',
        'SPATIAL_CRS_MISMATCH',
        'GRAPH_PATH_NOT_FOUND',

        // Evidence-quality codes (6)
        'NUMERIC_GROUNDING_FAILED',
        'CITATION_INCOMPLETE',
        'CONFLICTING_SOURCES',
        'MISSING_DEPTH_INTERVAL',
        'MISSING_ASSAY_UNITS',
        'SOURCE_SCOPE_VIOLATION',

        // Query-failure code (1)
        'UNSUPPORTED_QUERY_TYPE',

        // Out-of-band (death loop)
        'DEATH_LOOP',

        // Degradation variants
        'ENTITY_NOT_FOUND_NO_ALIASES',
        'CONFLICTING_SOURCES_WITH_AUTHORITY',
    ];

    /**
     * Render one code into a user-facing string.
     *
     * @param string $code A GuardErrorCode value (e.g. 'NO_EVIDENCE_FOUND').
     *                     Unknown codes return a generic fallback.
     * @param array<string, string|int|null> $placeholders Values for
     *                                                     `:placeholder` markers in the translation
     *                                                     template. Missing placeholders are left
     *                                                     as the literal `:name` (Laravel default).
     */
    public function render(string $code, array $placeholders = []): string
    {
        $effective = $this->dispatchVariant($code, $placeholders);

        if (! in_array($effective, self::KNOWN_CODES, true)) {
            return __('guard_errors.UNSUPPORTED_QUERY_TYPE', [
                'reason' => "internal: unknown guard code '{$code}'",
                'specific_alternative_action' => 'rephrase your question',
            ]);
        }

        // Coerce nulls to empty strings — Laravel's :placeholder replacer
        // refuses null and throws.
        $clean = [];
        foreach ($placeholders as $k => $v) {
            $clean[$k] = $v ?? '';
        }

        return (string) __('guard_errors.'.$effective, $clean);
    }

    /**
     * Render multiple codes; useful when a response carries several
     * codes that all fired in the same query (composite signals).
     *
     * @param array<int, string> $codes
     * @param array<string, string|int|null> $placeholders
     *
     * @return array<int, string>
     */
    public function renderMany(array $codes, array $placeholders = []): array
    {
        return array_map(fn (string $code) => $this->render($code, $placeholders), $codes);
    }

    /**
     * True when the code (or its degradation variant) is in the known
     * set. Callers use this to validate FastAPI payloads before
     * forwarding to the React side.
     */
    public function isKnownCode(string $code): bool
    {
        return in_array($code, self::KNOWN_CODES, true);
    }

    /**
     * Maps the canonical code to its degradation variant when the
     * placeholders indicate it. Otherwise returns the code unchanged.
     */
    private function dispatchVariant(string $code, array $placeholders): string
    {
        if ($code === 'ENTITY_NOT_FOUND') {
            $aliases = $placeholders['suggested_aliases'] ?? null;
            if ($aliases === null || $aliases === '' || $aliases === []) {
                return 'ENTITY_NOT_FOUND_NO_ALIASES';
            }
        }

        if ($code === 'CONFLICTING_SOURCES') {
            $authoritative = $placeholders['authoritative_doc'] ?? null;
            if (! empty($authoritative)) {
                return 'CONFLICTING_SOURCES_WITH_AUTHORITY';
            }
        }

        return $code;
    }
}
