<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Log;
use Symfony\Component\HttpFoundation\Response;

/**
 * Symmetric shared-secret auth for FastAPI → Laravel internal callbacks.
 *
 * The same `FASTAPI_SERVICE_KEY` env var that Laravel uses to call FastAPI
 * is used in reverse for the small set of internal endpoints FastAPI calls
 * back into (e.g. real-time progress broadcasts that ride Laravel Reverb).
 *
 * The key MUST be present and MUST match. Requests without the header or
 * with a mismatch get a 401. Constant-time compare via `hash_equals`.
 *
 * Rotation (2026-09-06): `services.fastapi.service_key_previous` — the
 * outgoing key — is accepted as well while it is set, mirroring
 * FastAPI's FASTAPI_SERVICE_KEY_PREVIOUS on the reverse path. Before this
 * the header was compared against the current key alone, so every
 * Hatchet / FastAPI call into Laravel 401'd between the Laravel apps
 * restarting on a new key and their callers restarting too. Both
 * candidates are compared on every request, not short-circuited, so the
 * response time does not say which one matched.
 */
class VerifyServiceKey
{
    /**
     * Whether this process has already logged a previous-key match. A
     * plain once-flag with no request data — one line per process during
     * a rotation window, and one line a week later is the signal that a
     * caller never received the new key.
     */
    private static bool $previousKeySeen = false;

    public function handle(Request $request, Closure $next): Response
    {
        $expected = (string) config('services.fastapi.service_key', '');
        $previous = (string) config('services.fastapi.service_key_previous', '');
        $supplied = (string) $request->header('X-Service-Key', '');

        if ($expected === '' || $supplied === '') {
            return response()->json(['error' => 'invalid service key'], 401);
        }

        $matchesCurrent = hash_equals($expected, $supplied);
        $matchesPrevious = $previous !== '' && hash_equals($previous, $supplied);

        if (! $matchesCurrent && ! $matchesPrevious) {
            return response()->json(['error' => 'invalid service key'], 401);
        }

        if ($matchesPrevious && ! $matchesCurrent && ! self::$previousKeySeen) {
            self::$previousKeySeen = true;
            Log::warning(
                'X-Service-Key authenticated with FASTAPI_SERVICE_KEY_PREVIOUS — '
                .'a caller is still on the outgoing key. Expected during the '
                .'rotation window; a consumer was missed if this persists.',
            );
        }

        return $next($request);
    }
}
