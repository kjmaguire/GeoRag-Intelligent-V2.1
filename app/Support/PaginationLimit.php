<?php

declare(strict_types=1);

namespace App\Support;

use Illuminate\Http\Request;

/**
 * One place that decides how many rows a caller may ask for.
 *
 * Three apiResource index endpoints passed `$request->integer('per_page')`
 * straight into `paginate()` with no ceiling, and PublicApiController::reports()
 * clamped with `min($limit, 200)` — which looks like a ceiling and is not:
 * Laravel's `Builder::limit()` silently ignores a negative value, so
 * `?limit=-1` removed the LIMIT clause altogether.
 *
 * `GET /api/v1/reports?limit=-1` therefore returned every row the caller could
 * see, with no LIMIT, materialised into PHP memory behind a 512M limit, pinning
 * one of four Octane workers until it OOMed. `?per_page=1000000` on the collars
 * endpoint has the same shape. Both are reachable by any authenticated member.
 */
final class PaginationLimit
{
    /** Nobody has a legitimate reason to pull more than this in one request. */
    public const MAX = 200;

    /**
     * Clamp a caller-supplied page size into [1, MAX].
     *
     * Handles the three ways the input goes wrong: absent (use the default),
     * zero or negative (which is what disabled the LIMIT), and absurdly large.
     */
    public static function clamp(Request $request, int $default, string $key = 'per_page'): int
    {
        $requested = $request->integer($key, $default);

        if ($requested < 1) {
            return $default;
        }

        return min($requested, self::MAX);
    }
}
