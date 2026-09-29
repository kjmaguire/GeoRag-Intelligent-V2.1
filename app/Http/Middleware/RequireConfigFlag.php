<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
use Symfony\Component\HttpFoundation\Response;

/**
 * 404 unless a boolean config flag is on.
 *
 * For routed features that are dormant in some deployments: with the flag
 * off the route answers exactly like a route that does not exist, rather
 * than 500/503 from a half-configured feature. Usage:
 *
 *     ->middleware(RequireConfigFlag::class.':services.cloud_ingest_oauth.enabled')
 *
 * Reads config per request, so a test (or an operator redeploy) can flip
 * the flag without re-registering routes, and `route:cache` stays valid.
 * Octane-safe: stateless.
 */
class RequireConfigFlag
{
    /**
     * @param Closure(Request): (Response) $next
     */
    public function handle(Request $request, Closure $next, string $configKey): Response
    {
        if (config($configKey) !== true) {
            abort(404);
        }

        return $next($request);
    }
}
