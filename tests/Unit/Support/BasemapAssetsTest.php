<?php

declare(strict_types=1);

namespace Tests\Unit\Support;

use App\Http\Middleware\SecurityHeadersMiddleware;
use App\Support\BasemapAssets;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * The map surfaces, the CSP and the preconnect hints must name the same
 * hosts.
 *
 * They did not. connect-src carried four literal hosts; app.blade.php
 * preconnected to three, two of which (the terrain DEM and the Sentinel-2
 * imagery MapView uses) were absent from connect-src entirely. So the page
 * opened a TLS connection to a host the browser then refused to fetch from,
 * and terrain / satellite mode was broken in production while working in
 * local dev, where no CSP applies to a plain `php artisan serve`.
 *
 * The regression these pin is narrow and easy to reintroduce: adding a map
 * asset in a component and forgetting one of the two allowlists.
 */
final class BasemapAssetsTest extends TestCase
{
    #[Test]
    public function it_covers_every_map_asset_the_frontend_can_fetch(): void
    {
        // One entry per network dependency the map code reaches for:
        // three basemap styles, the glyph endpoint, the Workspace satellite
        // raster, MapView's terrain DEM and MapView's imagery raster.
        $urls = BasemapAssets::urls();

        $this->assertCount(7, $urls, 'a map asset was added to config without a reader, or vice versa');
    }

    #[Test]
    public function the_terrain_and_imagery_hosts_are_allowed_by_the_csp(): void
    {
        // The specific bug: MapView added terrain + satellite, app.blade.php
        // got preconnect hints for both, and connect-src did not.
        $csp = (new SecurityHeadersMiddleware)->buildCsp('production');

        $this->assertStringContainsString('https://tiles.mapterhorn.com', $csp);
        $this->assertStringContainsString('https://tiles.maps.eox.at', $csp);
    }

    #[Test]
    public function every_preconnect_host_is_also_a_csp_source(): void
    {
        // Preconnecting to a host the CSP forbids is strictly worse than
        // not preconnecting: it costs a DNS lookup and a TLS handshake for
        // a connection the browser will never use.
        $csp = (new SecurityHeadersMiddleware)->buildCsp('production');

        foreach (BasemapAssets::origins() as $origin) {
            $this->assertStringContainsString(
                $origin,
                $csp,
                "app.blade.php preconnects to {$origin} but the CSP does not allow it",
            );
        }
    }

    #[Test]
    public function origins_drop_the_path_and_keep_the_port(): void
    {
        config()->set('services.basemap.styles', [
            'positron' => 'https://tiles.internal.example:8443/styles/positron/style.json',
        ]);
        config()->set('services.basemap.glyphs', null);
        config()->set('services.basemap.satellite_tiles', null);
        config()->set('services.basemap.dem_tiles', null);
        config()->set('services.basemap.imagery_tiles', null);

        $this->assertSame(['https://tiles.internal.example:8443'], BasemapAssets::origins());
    }

    #[Test]
    public function a_same_origin_tile_server_contributes_nothing(): void
    {
        // A relative URL is served by the app itself, which 'self' already
        // covers — and preconnecting to your own origin is pointless.
        config()->set('services.basemap.styles', ['positron' => '/tiles/positron.json']);
        config()->set('services.basemap.glyphs', '');
        config()->set('services.basemap.satellite_tiles', null);
        config()->set('services.basemap.dem_tiles', null);
        config()->set('services.basemap.imagery_tiles', null);

        $this->assertSame([], BasemapAssets::origins());
        $this->assertSame([], BasemapAssets::cspSources());
    }

    #[Test]
    public function csp_sources_add_a_sibling_subdomain_wildcard(): void
    {
        config()->set('services.basemap.styles', ['dark_matter' => 'https://basemaps.example.com/style.json']);
        config()->set('services.basemap.glyphs', null);
        config()->set('services.basemap.satellite_tiles', null);
        config()->set('services.basemap.dem_tiles', null);
        config()->set('services.basemap.imagery_tiles', null);

        $this->assertSame(
            ['https://basemaps.example.com', 'https://*.basemaps.example.com'],
            BasemapAssets::cspSources(),
        );
    }
}
