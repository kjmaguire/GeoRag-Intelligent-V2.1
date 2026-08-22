/**
 * MapLibre basemap style URL accessor.
 *
 * Reads from Inertia shared props (`basemap_styles`) populated by
 * `HandleInertiaRequests::share()` and ultimately from
 * `config/services.php` → `basemap.styles`.
 *
 * Why a hook + central registry:
 * ─────────────────────────────
 *   - Per CLAUDE.md hard rule #8, GeoRAG uses MapLibre GL (NOT Mapbox GL)
 *     so on-prem deployments can run fully air-gapped. The basemap style
 *     URL is the ONE thing maplibre-gl reaches out to over the network —
 *     centralising it makes on-prem swap a one-env-var change.
 *   - Multiple components (PublicGeoscienceMap, MapView, AoiMap, Analytics
 *     AlterationMap) used to hardcode the same URLs. Drift was inevitable.
 *
 * Adding a new style:
 *   1. Add the env var to .env.example
 *   2. Add the entry to config/services.php → basemap.styles
 *   3. Add the key to HandleInertiaRequests::share() (covered by the
 *      `config('services.basemap.styles')` spread — no edit needed)
 *   4. Add the key to BasemapStyleId below
 *
 * 2026-08-22: step 2 named a config block that did not exist, so the shared
 * prop was null on every response and every map used the fallbacks below —
 * the air-gap swap this module exists to enable could not be performed.
 * The block is now there, and the two assets that were NOT in the registry
 * (the glyph endpoint and the satellite raster template, both hard-coded a
 * second time inside WorkspaceMap) are configured alongside the styles.
 */
import { usePage } from '@inertiajs/react';

export type BasemapStyleId = 'positron' | 'bright' | 'dark_matter';

/**
 * Defaults that mirror config/services.php. Used as a last-resort fallback
 * when the Inertia shared prop is missing (e.g., during Storybook isolation
 * or a unit test that doesn't render through Inertia).
 */
const DEFAULT_STYLE_URLS: Record<BasemapStyleId, string> = {
    positron:    'https://tiles.openfreemap.org/styles/positron',
    bright:      'https://tiles.openfreemap.org/styles/bright',
    dark_matter: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
};

interface SharedPropsWithBasemap {
    basemap_styles?: Partial<Record<BasemapStyleId, string>> | null;
    basemap_glyphs?: string | null;
    basemap_satellite?: { tiles?: string | null; attribution?: string | null } | null;
    basemap_dem?: string | null;
    basemap_imagery?: string | null;
    [key: string]: unknown;
}

/**
 * Font-PBF endpoint for styles we build inline rather than fetch.
 * MapLibre needs one for any style object that renders text.
 */
const DEFAULT_GLYPHS_URL =
    'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/glyphs/{fontstack}/{range}.pbf';

/** Esri World Imagery — free, no key. */
const DEFAULT_SATELLITE_TILES =
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';

const DEFAULT_SATELLITE_ATTRIBUTION = 'Tiles © Esri';

/** Terrain-RGB DEM used by MapView's terrain mode. */
const DEFAULT_DEM_URL = 'https://tiles.mapterhorn.com/tilejson.json';

/** Sentinel-2 cloudless imagery used by MapView's satellite mode. */
const DEFAULT_IMAGERY_TILES =
    'https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg';

/**
 * Returns the configured style.json URL for a named basemap.
 *
 * @example
 *   const style = useBasemapStyleUrl('positron');
 *   new maplibregl.Map({ container, style, ... });
 */
export function useBasemapStyleUrl(id: BasemapStyleId): string {
    const page = usePage<SharedPropsWithBasemap>();
    const fromProps = page.props.basemap_styles?.[id];
    return fromProps ?? DEFAULT_STYLE_URLS[id];
}

/**
 * Glyph (font PBF) endpoint for hand-built style objects.
 */
export function useBasemapGlyphsUrl(): string {
    const page = usePage<SharedPropsWithBasemap>();
    return page.props.basemap_glyphs ?? DEFAULT_GLYPHS_URL;
}

/**
 * Raster tile template + attribution for the satellite basemap, which is a
 * tile source rather than a style.json and so cannot go through
 * useBasemapStyleUrl.
 */
export function useSatelliteTiles(): { tiles: string; attribution: string } {
    const page = usePage<SharedPropsWithBasemap>();
    const configured = page.props.basemap_satellite;
    return {
        tiles: configured?.tiles ?? DEFAULT_SATELLITE_TILES,
        attribution: configured?.attribution ?? DEFAULT_SATELLITE_ATTRIBUTION,
    };
}

/**
 * Terrain-RGB DEM tilejson for MapView's terrain mode.
 *
 * `VITE_DEM_TILES_URL` wins when it is set. That is deliberate and it is
 * the opposite of the usual runtime-beats-build-time ordering: a
 * deployment that set the Vite variable did so explicitly, and the server
 * value has a shipped default, so preferring the server value would
 * silently replace a self-hosted DEM with the public one. New deployments
 * should use BASEMAP_DEM_TILES, which needs no rebuild — the comment above
 * these constants in MapView claimed the Vite variables gave you that, and
 * `import.meta.env` substitutes at build time, so they never did.
 */
export function useTerrainDemUrl(): string {
    const page = usePage<SharedPropsWithBasemap>();
    const baked = import.meta.env.VITE_DEM_TILES_URL as string | undefined;
    return baked || page.props.basemap_dem || DEFAULT_DEM_URL;
}

/**
 * Satellite imagery raster template for MapView.
 *
 * Note this is a different provider from useSatelliteTiles(), which serves
 * the Workspace map. See config/services.php.
 */
export function useImageryTileUrl(): string {
    const page = usePage<SharedPropsWithBasemap>();
    const baked = import.meta.env.VITE_SATELLITE_TILES_URL as string | undefined;
    return baked || page.props.basemap_imagery || DEFAULT_IMAGERY_TILES;
}
