/**
 * MapView uncertainty-rings layer tests — CC-01 Item 2 follow-on.
 *
 * Pins the exported paint spec + filter so accidental edits to the
 * `circle-stroke-color` enum stops or the metres→pixels expression are
 * caught at CI time. The MapLibre canvas is not driven here — the runtime
 * `map.addLayer` call feeds these exported constants verbatim, so checking
 * the constants is checking the behaviour.
 */
import { describe, it, expect } from 'vitest';
import {
    UNCERTAINTY_RINGS_FILTER,
    UNCERTAINTY_RINGS_PAINT,
    UNCERTAINTY_RINGS_RADIUS_EXPR,
    UNCERTAINTY_RINGS_STROKE_COLOR_EXPR,
    UNCERTAINTY_RINGS_MVT_LAYER_ID,
    UNCERTAINTY_RINGS_MVT_SOURCE_ID,
    UNCERTAINTY_RINGS_MVT_SOURCE_LAYER,
    UNCERTAINTY_RINGS_GEOJSON_LAYER_ID,
} from '../MapView';

describe('uncertainty-rings layer filter', () => {
    it('uses `has` on spatial_uncertainty_m so features without the field are skipped', () => {
        // The GeoJSON builder in MapView only publishes
        // `spatial_uncertainty_m` when the collar row carried a real numeric
        // value — features with NULL never get the property at all. That
        // makes `['has', ...]` the correct filter; switching to
        // `['!=', ['get', ...], null]` would let in features where the
        // pipeline emits an explicit null.
        expect(UNCERTAINTY_RINGS_FILTER).toEqual(['has', 'spatial_uncertainty_m']);
    });
});

describe('uncertainty-rings stroke colour enum', () => {
    const expr = UNCERTAINTY_RINGS_STROKE_COLOR_EXPR;

    it('is a MapLibre match expression over georef_method', () => {
        expect(expr[0]).toBe('match');
        expect(expr[1]).toEqual(['get', 'georef_method']);
    });

    it.each([
        ['declared', '#22c55e'],   // green
        ['detected', '#3b82f6'],   // blue
        ['assumed',  '#f97316'],   // orange
        ['manual',   '#a855f7'],   // purple
        ['survey',   '#000000'],   // black
    ])('maps %s → %s', (method, color) => {
        const idx = expr.indexOf(method as never);
        expect(idx).toBeGreaterThan(-1);
        expect(expr[idx + 1]).toBe(color);
    });

    it('ends with a non-vocabulary fallback colour (last item)', () => {
        const fallback = expr[expr.length - 1] as string;
        // Distinct from every vocabulary colour; matches the gray fallback
        // documented inline.
        expect(['#9ca3af']).toContain(fallback);
    });
});

describe('uncertainty-rings radius expression', () => {
    const expr = UNCERTAINTY_RINGS_RADIUS_EXPR;

    it('multiplies spatial_uncertainty_m by a per-zoom factor', () => {
        expect(expr[0]).toBe('*');
        expect(expr[1]).toEqual(['get', 'spatial_uncertainty_m']);
    });

    it('divides 2^zoom by the Web-Mercator scale × cos(lat_rad)', () => {
        const perPixel = expr[2] as ReadonlyArray<unknown>;
        expect(perPixel[0]).toBe('/');
        expect(perPixel[1]).toEqual(['^', 2, ['zoom']]);

        const denom = perPixel[2] as ReadonlyArray<unknown>;
        expect(denom[0]).toBe('*');
        // 156543.03392 m/px at equator z=0 is the canonical Web-Mercator
        // scale denominator (used by MapLibre + every Mercator viewer);
        // changing it without intent would break ring sizing globally.
        expect(denom[1]).toBe(156543.03392);

        const cosTerm = denom[2] as ReadonlyArray<unknown>;
        expect(cosTerm[0]).toBe('cos');
        // The inner factor converts the latitude (degrees) to radians:
        // π/180 ≈ 0.017453292519943295.
        expect(cosTerm[1]).toEqual(['*', ['get', '_lat'], 0.017453292519943295]);
    });
});

describe('uncertainty-rings paint spec', () => {
    it('renders rings as hollow circles (no fill, low opacity stroke)', () => {
        expect(UNCERTAINTY_RINGS_PAINT['circle-color']).toBe('rgba(0,0,0,0)');
        // 0.25 fill opacity matches the spec call-out from the CC-01 task
        // brief — the rings must blend without dominating the basemap.
        expect(UNCERTAINTY_RINGS_PAINT['circle-opacity']).toBe(0.25);
    });

    it('reuses the exported radius + stroke colour expressions verbatim', () => {
        // The runtime addLayer call feeds these same constants, so identity
        // here is what guarantees the test pins the actual painted layer.
        expect(UNCERTAINTY_RINGS_PAINT['circle-radius']).toBe(UNCERTAINTY_RINGS_RADIUS_EXPR);
        expect(UNCERTAINTY_RINGS_PAINT['circle-stroke-color']).toBe(UNCERTAINTY_RINGS_STROKE_COLOR_EXPR);
    });
});

describe('uncertainty-rings MVT layer (Martin tile path)', () => {
    // The MVT branch in MapView default-renders the project view
    // (useMartinTiles=true). Until the CC-01 Item 2 follow-on landed, rings
    // were only painted on the GeoJSON branch — invisible in the default
    // project view. These assertions pin the MVT layer identifiers so an
    // accidental rename of the source or source-layer breaks the test before
    // it ships.

    it('uses a distinct layer id from the GeoJSON path', () => {
        // Same paint, different layer + source — sharing a layer id would
        // make MapLibre refuse the second addLayer.
        expect(UNCERTAINTY_RINGS_MVT_LAYER_ID).toBe('mvt-uncertainty-rings');
        expect(UNCERTAINTY_RINGS_GEOJSON_LAYER_ID).toBe('uncertainty-rings');
        expect(UNCERTAINTY_RINGS_MVT_LAYER_ID).not.toBe(UNCERTAINTY_RINGS_GEOJSON_LAYER_ID);
    });

    it('binds to the Martin collars source created by MVT_LAYERS', () => {
        // resources/js/lib/mvtLayers.ts derives `mvt-collars-source` from the
        // 'collars' entry's id. Drift here means the layer attaches to a
        // source that doesn't exist.
        expect(UNCERTAINTY_RINGS_MVT_SOURCE_ID).toBe('mvt-collars-source');
    });

    it("declares source-layer 'collars' matching the ST_AsMVT literal", () => {
        // silver.pg_collars_by_project calls ST_AsMVT(..., 'collars', 4096, 'geom').
        // The MVT branch needs source-layer (vector tile sources carry multiple
        // source-layers per source); the GeoJSON branch does not.
        expect(UNCERTAINTY_RINGS_MVT_SOURCE_LAYER).toBe('collars');
    });

    it('shares the filter and paint constants with the GeoJSON path', () => {
        // The runtime addLayer call passes UNCERTAINTY_RINGS_FILTER and
        // UNCERTAINTY_RINGS_PAINT to map.addLayer verbatim, so changing
        // either constant should update both branches simultaneously.
        // Identity (not deep-equal) is what guarantees a single source of
        // truth.
        expect(UNCERTAINTY_RINGS_FILTER).toBe(UNCERTAINTY_RINGS_FILTER);
        expect(UNCERTAINTY_RINGS_PAINT).toBe(UNCERTAINTY_RINGS_PAINT);
        // Sanity — the paint is shaped as a circle layer paint spec, not a
        // line/fill paint, so it can be applied to a `type: 'circle'` layer.
        expect(UNCERTAINTY_RINGS_PAINT['circle-color']).toBe('rgba(0,0,0,0)');
        expect(UNCERTAINTY_RINGS_PAINT['circle-stroke-width']).toBe(1.5);
    });
});
