/**
 * GIS-5 — the uncertainty-ring paint must pass MapLibre's own style
 * validation. A layer that fails validation is dropped by addLayer with only
 * an `error` event, which both call sites swallowed, so "the constants look
 * right" was never evidence the ring rendered.
 */
import { describe, expect, it } from 'vitest';
import { validateStyleMin } from '@maplibre/maplibre-gl-style-spec';
import {
    UNCERTAINTY_RINGS_FILTER,
    UNCERTAINTY_RINGS_PAINT,
    WEB_MERCATOR_M_PER_PX_Z0_512,
    uncertaintyRingRadiusPx,
} from '@/lib/uncertaintyRings';

function styleWith(layer: Record<string, unknown>) {
    return {
        version: 8,
        sources: {
            collars: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
            'mvt-collars-source': { type: 'vector', tiles: ['https://example.test/{z}/{x}/{y}.pbf'] },
        },
        layers: [layer],
    };
}

describe('uncertainty-rings paint validates against the MapLibre style spec', () => {
    it('GeoJSON layer has no validation errors', () => {
        const errors = validateStyleMin(styleWith({
            id: 'uncertainty-rings',
            type: 'circle',
            source: 'collars',
            filter: UNCERTAINTY_RINGS_FILTER,
            paint: UNCERTAINTY_RINGS_PAINT,
        }) as never);
        expect(errors.map((e) => e.message)).toEqual([]);
    });

    it('MVT layer has no validation errors', () => {
        const errors = validateStyleMin(styleWith({
            id: 'mvt-uncertainty-rings',
            type: 'circle',
            source: 'mvt-collars-source',
            'source-layer': 'collars',
            filter: UNCERTAINTY_RINGS_FILTER,
            paint: UNCERTAINTY_RINGS_PAINT,
        }) as never);
        expect(errors.map((e) => e.message)).toEqual([]);
    });

    it('rejects the pre-fix expression, proving the validator catches it', () => {
        const errors = validateStyleMin(styleWith({
            id: 'old',
            type: 'circle',
            source: 'collars',
            paint: {
                'circle-radius': ['*', ['get', 'spatial_uncertainty_m'],
                    ['/', ['^', 2, ['zoom']], ['*', 156543.03392, ['cos', ['*', ['get', '_lat'], 0.0174]]]]],
            },
        }) as never);
        expect(errors.length).toBeGreaterThan(0);
    });
});

describe('uncertaintyRingRadiusPx', () => {
    it('is the true on-screen radius for 512-px tiles', () => {
        // At the equator, zoom 0, 78271.517 m is exactly one pixel.
        expect(uncertaintyRingRadiusPx(WEB_MERCATOR_M_PER_PX_Z0_512, 0, 0)).toBeCloseTo(1, 9);
        // Each zoom level doubles the radius.
        expect(uncertaintyRingRadiusPx(100, 58, 14) / uncertaintyRingRadiusPx(100, 58, 13)).toBeCloseTo(2, 9);
        // 100 m at 60°N, zoom 15: 100 · 32768 / (78271.517 · 0.5) ≈ 83.7 px.
        expect(uncertaintyRingRadiusPx(100, 60, 15)).toBeCloseTo(83.73, 1);
    });
});
