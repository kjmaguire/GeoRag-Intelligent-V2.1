<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use App\Support\BasemapAssets;
use Closure;
use Illuminate\Http\Request;
use Symfony\Component\HttpFoundation\Response;

/**
 * Module 9 Chunk 9.5 — emit defence-in-depth security headers on every
 * response. Closes audit findings A5-01 and A5-02.
 *
 * Always-on headers
 * -----------------
 *   X-Frame-Options: DENY
 *   X-Content-Type-Options: nosniff
 *   Referrer-Policy: strict-origin-when-cross-origin
 *   Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=()
 *   Content-Security-Policy: <see CSP_DIRECTIVES below>
 *
 * Conditional headers
 * -------------------
 *   Strict-Transport-Security — only on HTTPS requests, 1-year max-age
 *                              with includeSubDomains. Skipped on http://
 *                              so local dev stays unbroken.
 *
 * CSP scope
 * ---------
 *   Inertia + Vite + MapLibre GL + Plotly + React Flow + tile proxy + SSE.
 *   `'unsafe-inline'` and `'unsafe-eval'` remain on script-src because Vite
 *   dev mode and the Inertia bridge inject inline scripts. Module 10 polish
 *   should migrate to nonce-based directives once the build pipeline emits
 *   stable nonces.
 *
 * Octane-safe: middleware holds no per-request state. The CSP string is
 * built lazily inside handle() so $request->isSecure() reflects the
 * current request, not boot-time state.
 */
final class SecurityHeadersMiddleware
{
    /**
     * Always-on header set. Strict-Transport-Security is added separately
     * because it depends on the request scheme.
     *
     * @var array<string,string>
     */
    private const ALWAYS_HEADERS = [
        'X-Frame-Options' => 'DENY',
        'X-Content-Type-Options' => 'nosniff',
        'Referrer-Policy' => 'strict-origin-when-cross-origin',
        'Permissions-Policy' => 'geolocation=(), microphone=(), camera=(), payment=()',
    ];

    /**
     * connect-src origins that are not derivable from configuration.
     *
     * `demotiles.maplibre.org` is MapLibre's own built-in fallback style,
     * used when a configured style fails to load. `s3.amazonaws.com` is the
     * presigned-download host for exports.
     *
     * @var list<string>
     */
    private const STATIC_CONNECT_ORIGINS = [
        'https://demotiles.maplibre.org',
        'https://s3.amazonaws.com',
    ];

    public function handle(Request $request, Closure $next): Response
    {
        /** @var Response $response */
        $response = $next($request);

        foreach (self::ALWAYS_HEADERS as $name => $value) {
            // Don't clobber a header a downstream layer (Octane swap, Inertia)
            // explicitly set. Use setIfAbsent semantics via has().
            if (! $response->headers->has($name)) {
                $response->headers->set($name, $value);
            }
        }

        if ($request->isSecure() && ! $response->headers->has('Strict-Transport-Security')) {
            $response->headers->set(
                'Strict-Transport-Security',
                'max-age=31536000; includeSubDomains',
            );
        }

        if (! $response->headers->has('Content-Security-Policy')) {
            $response->headers->set(
                'Content-Security-Policy',
                $this->buildCsp(app()->environment()),
            );
        }

        return $response;
    }

    /**
     * Build the CSP string. Kept as a method (not constant) so the
     * `upgrade-insecure-requests` directive can be conditional on the
     * runtime environment.
     */
    public function buildCsp(string $env): string
    {
        $directives = [
            "default-src 'self'",
            // Vite dev server + Inertia bridge inject inline scripts.
            // `'unsafe-eval'` is required by MapLibre's worker shim and
            // some plotly evaluation paths. Module 10 should tighten to
            // nonce-based directives.
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
            // Tailwind + shadcn require inline styles; fonts.bunny.net
            // hosts the Figtree + Instrument Sans webfonts referenced
            // by app.blade.php / welcome.blade.php.
            "style-src 'self' 'unsafe-inline' https://fonts.bunny.net",
            // Raster tiles (MapLibre) + plot images can come from any HTTPS
            // source; data: URIs are used for inline SVGs.
            "img-src 'self' data: blob: https:",
            // Reverb WebSocket + SSE + tile proxy + FastAPI + the MapLibre
            // style / tile-JSON fetches.
            //
            // The basemap origins are DERIVED from config('services.basemap')
            // rather than listed. They used to be four literals with a
            // comment saying "add new tile providers here as we onboard",
            // which made repointing a basemap a two-file change where only
            // one of the two was discoverable: an operator who set
            // BASEMAP_STYLE_POSITRON to their own tile server got a style
            // fetch blocked by a CSP they had no reason to look at. The
            // whole point of that indirection is the air-gapped deployment
            // (CLAUDE.md hard rule #8), and a hard-coded allowlist defeats
            // it just as thoroughly as a hard-coded URL.
            'connect-src '.implode(' ', array_merge(
                ["'self'", 'wss:', 'ws:'],
                self::STATIC_CONNECT_ORIGINS,
                BasemapAssets::cspSources(),
            )),
            // fonts.bunny.net serves the actual .woff2 binaries.
            "font-src 'self' data: https://fonts.bunny.net",
            // MapLibre uses worker scripts from blob: URLs.
            "worker-src 'self' blob:",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ];

        // Only enable upgrade-insecure-requests off-local. Local dev hits
        // http://localhost:8888 and would otherwise be force-upgraded to
        // HTTPS that the dev server doesn't speak.
        if ($env !== 'local') {
            $directives[] = 'upgrade-insecure-requests';
        }

        return implode('; ', $directives);
    }
}
