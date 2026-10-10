<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use App\Support\BasemapAssets;
use Closure;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\URL;
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
 *   Strict-Transport-Security — only when the BROWSER reached us over https,
 *                              1-year max-age with includeSubDomains. See
 *                              servedOverHttps(): behind CloudFront the
 *                              request itself arrives as http and
 *                              $request->isSecure() is false. Skipped on
 *                              real http:// so local dev stays unbroken.
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
     * used when a configured style fails to load. The presigned-download
     * hosts for object storage are NOT here: they are derived from the disk
     * config by objectStorageOrigins().
     *
     * @var list<string>
     */
    private const STATIC_CONNECT_ORIGINS = [
        'https://demotiles.maplibre.org',
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

        if ($this->servedOverHttps($request) && ! $response->headers->has('Strict-Transport-Security')) {
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
     * Whether the BROWSER reached this response over https, which is not the
     * same question as whether this process did.
     *
     * `$request->isSecure()` reads X-Forwarded-Proto, and with
     * `edge = "cloudfront"` (deploy/aws/terraform/edge.tf, the default) the
     * load balancer is the last proxy and its listener is plain HTTP, so it
     * truthfully reports `http` for a page the viewer loaded over https. HSTS
     * gated on isSecure() alone therefore vanishes entirely on the edge mode
     * that ships — silently, since every other header still appears.
     *
     * `URL::formatScheme()` is the scheme the application actually builds
     * links with: the forced one when AppServiceProvider has forced it,
     * otherwise the request's own. That is exactly the right question, and it
     * keeps the decision in one place rather than re-deriving APP_URL here.
     * Local development over http is unaffected — nothing forces a scheme
     * there, so this stays false.
     */
    private function servedOverHttps(Request $request): bool
    {
        return $request->isSecure() || URL::formatScheme() === 'https://';
    }

    /**
     * scheme://host[:port] for every object-storage disk that can mint a
     * presigned URL the browser is asked to load.
     *
     * Reads the same disk config `StorageService` resolves through, so the
     * allowlist cannot drift from the endpoint actually in use.
     *
     * One driver now, not two. `config/filesystems.php` resolved these disks
     * to `driver => 'azure'` when STORAGE_BACKEND=azure_blob until ADR-0022
     * retired that backend on 2026-09-08; the blob-host reader that fed this
     * list from `account_name`/`connection_string` went with it on
     * 2026-09-16, because no disk carries either key any more.
     *
     * The lesson it was written for still applies to whatever is added next:
     * this first shipped a `frame-src` holding no real origin at all, so the
     * directive was present and the header looked fixed while the Reports
     * "Original" iframe stayed blocked, because the host it actually loads
     * was never in the list. A new driver that names its host in some other
     * key has to be read here too.
     *
     * A disk with no configured endpoint is AWS itself, where the SDK derives
     * the URL from the bucket and region. That is what production is, and a
     * presigned URL there is NOT on `s3.amazonaws.com`: it is
     * `https://<bucket>.s3.<region>.amazonaws.com/...` (virtual-hosted), or
     * `https://s3.<region>.amazonaws.com/<bucket>/...` for a bucket name with a
     * dot in it. A CSP host-source matches the host exactly, so listing the
     * global host blocked the Reports "Original" iframe in the one deployment
     * that mattered. The hosts below are the ones the AWS SDK actually builds,
     * derived per disk from its bucket, region, endpoint and path-style
     * setting; a test builds a real presigned URL for each layout and checks
     * it is allowed, so a change in the SDK's layout fails there.
     *
     * @return list<string>
     */
    private static function objectStorageOrigins(): array
    {
        $origins = [];

        foreach (['s3', 's3-bronze', 's3-exports'] as $disk) {
            $origins = array_merge($origins, self::diskOrigins((array) config("filesystems.disks.{$disk}", [])));
        }

        return array_values(array_unique(array_filter($origins)));
    }

    /**
     * Every origin a presigned URL for one disk can be on.
     *
     * @param array<string, mixed> $disk a `filesystems.disks.*` entry
     *
     * @return list<string|null>
     */
    private static function diskOrigins(array $disk): array
    {
        // AWS_URL: a CDN or custom domain the deployment serves objects from.
        $origins = [self::originFromUrl($disk['url'] ?? null)];

        $bucket = self::dnsBucket($disk['bucket'] ?? null);
        $pathStyle = filter_var($disk['use_path_style_endpoint'] ?? false, FILTER_VALIDATE_BOOLEAN);

        $endpoint = $disk['endpoint'] ?? null;
        if (is_string($endpoint) && $endpoint !== '') {
            // Compose and on-prem (MinIO, SeaweedFS), or an AWS endpoint named
            // explicitly. Path style is <endpoint>/<bucket>/<key>; without it
            // the SDK moves the bucket into the host: <bucket>.<endpoint-host>.
            $origins[] = self::originFromUrl($endpoint);
            if ($bucket !== null && ! $pathStyle) {
                $origins[] = self::bucketHostOrigin($endpoint, $bucket);
            }

            return $origins;
        }

        // AWS itself. The partition decides the DNS suffix; us-east-1 is the
        // region the SDK still addresses through the global host.
        $region = is_string($disk['region'] ?? null) ? strtolower($disk['region']) : '';
        $suffix = str_starts_with($region, 'cn-') ? 'amazonaws.com.cn' : 'amazonaws.com';

        if (preg_match('/^[a-z0-9-]+$/', $region) === 1) {
            $origins[] = "https://s3.{$region}.{$suffix}";
            if ($bucket !== null) {
                $origins[] = "https://{$bucket}.s3.{$region}.{$suffix}";
            }
        }
        if ($region === '' || $region === 'us-east-1') {
            $origins[] = 'https://s3.amazonaws.com';
            if ($bucket !== null) {
                $origins[] = "https://{$bucket}.s3.amazonaws.com";
            }
        }

        return $origins;
    }

    /**
     * The bucket name when it can be a DNS label (virtual-hosted addressing
     * needs one), else null. A name with a dot is reached path-style on AWS.
     */
    private static function dnsBucket(mixed $bucket): ?string
    {
        return is_string($bucket) && preg_match('/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/', $bucket) === 1
            ? $bucket
            : null;
    }

    /**
     * scheme://<bucket>.<host>[:port] for an endpoint, as the SDK addresses a
     * virtual-hosted bucket on it; null when the endpoint has no host to
     * prefix (an IP address, where the SDK falls back to path style).
     */
    private static function bucketHostOrigin(string $endpoint, string $bucket): ?string
    {
        $parts = parse_url($endpoint);
        $scheme = $parts['scheme'] ?? null;
        $host = $parts['host'] ?? null;
        if ($scheme === null || $host === null || str_starts_with($host, '[') || filter_var($host, FILTER_VALIDATE_IP) !== false) {
            return null;
        }

        $port = isset($parts['port']) ? ':'.$parts['port'] : '';

        return "{$scheme}://{$bucket}.{$host}{$port}";
    }

    /**
     * scheme://host[:port] of a configured URL, or null when it names no host.
     *
     * A relative URL — which a same-origin deployment is entitled to
     * configure — yields null rather than a broken "://" token that would
     * invalidate the directive it lands in.
     */
    private static function originFromUrl(mixed $value): ?string
    {
        if (! is_string($value) || $value === '') {
            return null;
        }

        $parts = parse_url($value);
        $scheme = $parts['scheme'] ?? null;
        $host = $parts['host'] ?? null;
        if ($scheme === null || $host === null) {
            return null;
        }

        $port = isset($parts['port']) ? ':'.$parts['port'] : '';

        return "{$scheme}://{$host}{$port}";
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
            // by app.blade.php.
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
            //
            // The object-storage hosts are here too: connect-src has always
            // listed the presigned-download host, and a fetch() of a presigned
            // URL (as opposed to a navigation or an <iframe>) needs it.
            'connect-src '.implode(' ', array_merge(
                ["'self'", 'wss:', 'ws:'],
                self::STATIC_CONNECT_ORIGINS,
                self::objectStorageOrigins(),
                BasemapAssets::cspSources(),
            )),
            // fonts.bunny.net serves the actual .woff2 binaries.
            "font-src 'self' data: https://fonts.bunny.net",
            // MapLibre uses worker scripts from blob: URLs.
            "worker-src 'self' blob:",
            // The Reports "Original" tab embeds the source PDF in an
            // <iframe> pointed at a PRESIGNED object-storage URL, which is
            // a different origin from the app. With no frame-src directive
            // the browser falls back to `default-src 'self'` and refuses
            // it — Chrome renders "This content is blocked. Contact the
            // site owner to fix the issue.", which reads as a broken page
            // rather than as a policy decision, and the tab has therefore
            // never worked in deployment.
            //
            // Derived from the disk config for the same reason connect-src
            // derives its basemap origins: an air-gapped deployment points
            // its storage at its own endpoint (CLAUDE.md hard rule #8), and
            // a hard-coded `*.blob.core.windows.net` would break there
            // while looking correct here.
            'frame-src '.implode(' ', array_merge(
                ["'self'", 'blob:'],
                self::objectStorageOrigins(),
            )),
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
