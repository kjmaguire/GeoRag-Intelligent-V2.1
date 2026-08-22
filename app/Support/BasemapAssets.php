<?php

declare(strict_types=1);

namespace App\Support;

/**
 * Every network endpoint the map surfaces fetch, resolved from configuration.
 *
 * Three places need to agree about this list and none of them is a natural
 * owner of it:
 *
 *   * `SecurityHeadersMiddleware` has to allow the origins in connect-src,
 *   * `app.blade.php` preconnects to them so the first tile request skips
 *     DNS and TLS,
 *   * the SPA actually fetches them, via resources/js/lib/basemap.ts.
 *
 * They did not agree. connect-src listed four hosts as literals with a
 * comment saying "add new tile providers here as we onboard"; the Blade
 * template listed three, two of which were NOT in connect-src -- so the page
 * warmed a TLS connection to a host the browser then refused to fetch from,
 * and MapView's terrain and satellite modes were blocked in production.
 * Meanwhile the URLs themselves were hard-coded in two React components,
 * which is what the config block exists to prevent (CLAUDE.md hard rule #8:
 * an on-prem deployment must be able to point every one of these at its own
 * server and reach nothing public).
 *
 * So: one derivation, three readers.
 */
final class BasemapAssets
{
    /**
     * Every configured URL, in no particular order.
     *
     * Templated values ({z}/{x}/{y}, {fontstack}) are fine -- callers read
     * the authority, not the path.
     *
     * @return list<string>
     */
    public static function urls(): array
    {
        $urls = array_values((array) config('services.basemap.styles', []));
        $urls[] = config('services.basemap.glyphs');
        $urls[] = config('services.basemap.satellite_tiles');
        $urls[] = config('services.basemap.dem_tiles');
        $urls[] = config('services.basemap.imagery_tiles');

        return array_values(array_filter(
            $urls,
            static fn ($url): bool => is_string($url) && $url !== '',
        ));
    }

    /**
     * Concrete scheme://host[:port] for each configured URL.
     *
     * A value that is not an absolute URL -- a relative path to a tile
     * server on the app's own origin, say -- contributes nothing and needs
     * nothing: `'self'` already covers it, and preconnecting to your own
     * origin is pointless.
     *
     * @return list<string>
     */
    public static function origins(): array
    {
        $origins = [];

        foreach (self::urls() as $url) {
            $parts = parse_url($url);
            $scheme = $parts['scheme'] ?? null;
            $host = $parts['host'] ?? null;
            if ($scheme === null || $host === null) {
                continue;
            }
            $port = isset($parts['port']) ? ':'.$parts['port'] : '';
            $origins[] = "{$scheme}://{$host}{$port}";
        }

        return array_values(array_unique($origins));
    }

    /**
     * The same origins plus a sibling-subdomain wildcard for each.
     *
     * Tile CDNs shard across subdomains -- Carto's dark_matter style.json
     * points its tiles at a./b./c.basemaps.cartocdn.com -- so an allowlist
     * holding only the style host loads the style and then blocks every tile
     * it references. The literal list this replaced carried the wildcard for
     * exactly that reason.
     *
     * Only used for CSP. Preconnect takes {@see origins()}: you cannot
     * preconnect to a wildcard.
     *
     * @return list<string>
     */
    public static function cspSources(): array
    {
        $sources = [];

        foreach (self::origins() as $origin) {
            $sources[] = $origin;
            $sources[] = (string) preg_replace('#^(https?://)#', '$1*.', $origin);
        }

        return array_values(array_unique($sources));
    }
}
