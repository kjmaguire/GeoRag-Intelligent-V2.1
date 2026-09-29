/**
 * Collar uncertainty rings: one MapLibre paint spec, shared by MapView (GeoJSON
 * and MVT branches) and WorkspaceMap.
 *
 * ## Why this module exists (GIS-5, 2026-09-29 audit)
 *
 * The radius expression used to be
 *
 *     ['*', ['get', 'spatial_uncertainty_m'],
 *           ['/', ['^', 2, ['zoom']], ['*', 156543.03392, ['cos', …]]]]
 *
 * which is invalid: MapLibre only allows `["zoom"]` as the input of a
 * TOP-LEVEL `step` or `interpolate`. `addLayer` validates the style, emits an
 * `error` event and does not add the layer, and both call sites wrapped the
 * call in `try {} catch {}` — so the ring, the only on-map signal that a
 * collar's position rests on an ASSUMED CRS, never rendered anywhere.
 *
 * It also used 156543.03392 m/px, the zoom-0 resolution for 256-px tiles.
 * MapLibre's zoom levels are defined on 512-px tiles, whose zoom-0 resolution
 * is half that (78271.517 m/px), so even a valid version would have drawn
 * every ring at half its true radius.
 *
 * ## The expression
 *
 * On-screen pixels for a ground distance d (metres) at latitude φ and zoom z:
 *
 *     px = d · 2^z / (78271.517 · cos φ)
 *
 * That is exactly exponential in z with base 2, so an `interpolate
 * ['exponential', 2]` over two stops (z = 0 and z = 24) reproduces it at every
 * zoom in between — no approximation, and `["zoom"]` sits where the spec
 * requires it.
 */

/** Metres per pixel at zoom 0 on the equator for 512-px tiles (MapLibre). */
export const WEB_MERCATOR_M_PER_PX_Z0_512 = 78271.517;

/** Highest stop of the radius interpolation; MapLibre's max zoom is 24. */
export const UNCERTAINTY_RINGS_MAX_ZOOM = 24;

const DEG_TO_RAD = Math.PI / 180;

/** `78271.517 · cos(_lat)` — the per-feature zoom-0 ground resolution. */
const metresPerPixelAtZ0 = [
    '*',
    WEB_MERCATOR_M_PER_PX_Z0_512,
    ['cos', ['*', ['get', '_lat'], DEG_TO_RAD]],
] as const;

export const UNCERTAINTY_RINGS_FILTER = ['has', 'spatial_uncertainty_m'] as const;

export const UNCERTAINTY_RINGS_RADIUS_EXPR = [
    'interpolate',
    ['exponential', 2],
    ['zoom'],
    0,
    ['/', ['get', 'spatial_uncertainty_m'], metresPerPixelAtZ0],
    UNCERTAINTY_RINGS_MAX_ZOOM,
    [
        '/',
        ['*', ['get', 'spatial_uncertainty_m'], 2 ** UNCERTAINTY_RINGS_MAX_ZOOM],
        metresPerPixelAtZ0,
    ],
] as const;

export const UNCERTAINTY_RINGS_STROKE_COLOR_EXPR = [
    'match',
    ['get', 'georef_method'],
    'declared', '#22c55e',
    'detected', '#3b82f6',
    'assumed',  '#f97316',
    'manual',   '#a855f7',
    'survey',   '#000000',
    '#9ca3af',
] as const;

export const UNCERTAINTY_RINGS_PAINT = {
    'circle-color': 'rgba(0,0,0,0)',
    'circle-stroke-width': 1.5,
    'circle-opacity': 0.25,
    'circle-stroke-opacity': 0.55,
    'circle-radius': UNCERTAINTY_RINGS_RADIUS_EXPR,
    'circle-stroke-color': UNCERTAINTY_RINGS_STROKE_COLOR_EXPR,
} as const;

/**
 * Plain-JS evaluation of the radius, for tests and for anyone who wants to
 * sanity-check a ring size by hand. Mirrors UNCERTAINTY_RINGS_RADIUS_EXPR.
 */
export function uncertaintyRingRadiusPx(metres: number, latDeg: number, zoom: number): number {
    return (metres * 2 ** zoom) / (WEB_MERCATOR_M_PER_PX_Z0_512 * Math.cos(latDeg * DEG_TO_RAD));
}
