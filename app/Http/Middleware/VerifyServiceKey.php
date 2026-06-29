<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
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
 */
class VerifyServiceKey
{
    public function handle(Request $request, Closure $next): Response
    {
        $expected = config('services.fastapi.service_key', '');
        $supplied = (string) $request->header('X-Service-Key', '');

        if ($expected === '' || $supplied === '' || ! hash_equals($expected, $supplied)) {
            return response()->json(['error' => 'invalid service key'], 401);
        }

        return $next($request);
    }
}
