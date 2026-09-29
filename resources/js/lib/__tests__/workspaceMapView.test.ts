import { describe, expect, it } from 'vitest';
import { MVT_LAYERS } from '@/lib/mvtLayers';
import {
    DEFAULT_MAP_VIEW,
    NON_COLLAR_MAP_LAYER_IDS,
    hasNonCollarMapData,
    initialMapView,
} from '@/lib/workspaceMapView';

describe('initialMapView (FE-3)', () => {
    it('fits positioned collars first, padded', () => {
        const v = initialMapView([[-105, 58], [-104, 59]], [-120, 40, -110, 45]);
        expect(v.bounds).toEqual([-105.01, 57.99, -103.99, 59.01]);
    });

    it('falls back to the project extent when no collar has a position', () => {
        expect(initialMapView([], [-106.2, 57.1, -105.8, 57.4]).bounds).toEqual([-106.2, 57.1, -105.8, 57.4]);
    });

    it('pads a zero-area extent (a single point)', () => {
        expect(initialMapView([], [-106, 57, -106, 57]).bounds).toEqual([-106.01, 56.99, -105.99, 57.01]);
    });

    it('ignores an out-of-range extent and still returns a view', () => {
        // Model units stored as 4326 (a CAD file with no CRS) must not send
        // the map to longitude 512,100.
        const v = initialMapView([], [512100, 6123100, 512900, 6123900]);
        expect(v.bounds).toBeUndefined();
        expect(v.center).toEqual(DEFAULT_MAP_VIEW.center);
    });

    it('returns the default view with neither collars nor extent', () => {
        expect(initialMapView([], null)).toEqual({ center: DEFAULT_MAP_VIEW.center, zoom: DEFAULT_MAP_VIEW.zoom });
    });
});

describe('hasNonCollarMapData', () => {
    it('is true when an imported / MVT layer has rows', () => {
        expect(hasNonCollarMapData([{ id: 'collars', count: 0 }, { id: 'imported-polygons', count: 3 }])).toBe(true);
    });

    it('is false for collar-derived layers only', () => {
        expect(hasNonCollarMapData([{ id: 'collars', count: 0 }, { id: 'traces', count: 5 }, { id: 'geochem', count: 0 }])).toBe(false);
    });

    it('only names real MVT layer ids', () => {
        const mvtIds = new Set(MVT_LAYERS.map((l) => l.id));
        for (const id of NON_COLLAR_MAP_LAYER_IDS) {
            expect(mvtIds.has(id), id).toBe(true);
        }
    });
});
