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
    basemap_styles?: Partial<Record<BasemapStyleId, string>>;
    [key: string]: unknown;
}

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
