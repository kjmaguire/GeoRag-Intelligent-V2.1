/**
 * Where the Workspace map opens.
 *
 * Order of preference:
 *   1. the positioned collars, padded slightly (the historical behaviour);
 *   2. the project's non-collar extent from the server (spatial features,
 *      claims, geochem, formations, workings) — so a GIS-only delivery gets a
 *      map instead of "No drill data in this project" (FE-3);
 *   3. a neutral default view, so the map still exists and its MVT layers can
 *      still be panned to.
 */

/** [west, south, east, north] in degrees. */
export type LonLatBounds = [number, number, number, number];

export type InitialMapView =
    | { bounds: LonLatBounds; center?: undefined; zoom?: undefined }
    | { bounds?: undefined; center: [number, number]; zoom: number };

/** North-American overview; the platform's projects are all there today. */
export const DEFAULT_MAP_VIEW = { center: [-100, 55] as [number, number], zoom: 3 };

const COLLAR_PAD_DEG = 0.01;

function isValidBounds(b: LonLatBounds | null | undefined): b is LonLatBounds {
    if (!b || b.length !== 4 || b.some((v) => !Number.isFinite(v))) return false;
    const [w, s, e, n] = b;
    return w >= -180 && e <= 180 && s >= -90 && n <= 90 && w <= e && s <= n;
}

export function initialMapView(
    collarLngLats: ReadonlyArray<readonly [number, number]>,
    projectExtent: LonLatBounds | null | undefined,
): InitialMapView {
    if (collarLngLats.length > 0) {
        const lngs = collarLngLats.map((p) => p[0]);
        const lats = collarLngLats.map((p) => p[1]);
        return {
            bounds: [
                Math.min(...lngs) - COLLAR_PAD_DEG,
                Math.min(...lats) - COLLAR_PAD_DEG,
                Math.max(...lngs) + COLLAR_PAD_DEG,
                Math.max(...lats) + COLLAR_PAD_DEG,
            ],
        };
    }
    if (isValidBounds(projectExtent)) {
        const [w, s, e, n] = projectExtent;
        // A single point (one claim post, one sample) has a zero-area extent;
        // pad it like a lone collar so fitBounds does not zoom to the max.
        if (w === e || s === n) {
            return { bounds: [w - COLLAR_PAD_DEG, s - COLLAR_PAD_DEG, e + COLLAR_PAD_DEG, n + COLLAR_PAD_DEG] };
        }
        return { bounds: [w, s, e, n] };
    }
    return { center: DEFAULT_MAP_VIEW.center, zoom: DEFAULT_MAP_VIEW.zoom };
}

/**
 * Ids of the Layers-rail entries that are Martin MVT layers — data the map
 * shows without any collar. Kept beside initialMapView because together they
 * decide whether the Workspace canvas has anything to show.
 */
export const NON_COLLAR_MAP_LAYER_IDS: ReadonlySet<string> = new Set([
    'imported-points',
    'imported-lines',
    'imported-polygons',
    'geochem',
    'historic-workings',
    'formations',
    'boundaries',
    'seismic',
]);

/** True when the project has any map data that is not a collar. */
export function hasNonCollarMapData(layers: ReadonlyArray<{ id: string; count: number }>): boolean {
    return layers.some((l) => NON_COLLAR_MAP_LAYER_IDS.has(l.id) && l.count > 0);
}
