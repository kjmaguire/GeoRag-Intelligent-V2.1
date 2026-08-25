/**
 * The join between the MVT layer definitions and a MapLibre map.
 *
 * Written when Martin was restored (2026-08-25). The definitions and the URL
 * builder had existed for months while nothing added the sources to the real
 * map, so these tests pin the behaviours that made the gap invisible: source
 * sharing, idempotency, and toggling without re-adding.
 */
import { describe, expect, it } from 'vitest';

import { MVT_LAYERS, type MvtLayerDef } from '../mvtLayers';
import {
    addMvtLayers,
    mvtLayerId,
    mvtOutlineLayerId,
    removeMvtLayers,
    setMvtVisibility,
    type MvtCapableMap,
} from '../mvtSources';

/** A MapLibre stand-in that records what was asked of it. */
function fakeMap() {
    const sources = new Map<string, Record<string, unknown>>();
    const layers = new Map<string, Record<string, unknown>>();
    const visibility: Record<string, unknown> = {};
    const map: MvtCapableMap & {
        sources: typeof sources;
        layers: typeof layers;
        visibility: typeof visibility;
    } = {
        sources,
        layers,
        visibility,
        getSource: (id) => sources.get(id),
        addSource: (id, s) => {
            if (sources.has(id)) throw new Error(`duplicate source ${id}`);
            sources.set(id, s);
        },
        getLayer: (id) => layers.get(id),
        addLayer: (l) => {
            const id = l.id as string;
            if (layers.has(id)) throw new Error(`duplicate layer ${id}`);
            layers.set(id, l);
        },
        removeLayer: (id) => void layers.delete(id),
        removeSource: (id) => void sources.delete(id),
        setLayoutProperty: (layer, name, value) => {
            visibility[`${layer}.${name}`] = value;
        },
    };
    return map;
}

const OPTS = { projectId: 'p-1', dataVersion: 7 };

const spatial = MVT_LAYERS.filter(
    (l) => l.functionName === 'pg_spatial_features_by_project',
);

describe('addMvtLayers', () => {
    it('adds a style layer for every definition', () => {
        const map = fakeMap();
        addMvtLayers(map, OPTS);

        for (const def of MVT_LAYERS) {
            expect(map.layers.has(mvtLayerId(def))).toBe(true);
        }
    });

    it('opens ONE source for the three spatial-feature layers', () => {
        // pg_spatial_features_by_project emits imported_points/lines/polygons
        // in one tile. A source per definition fetches the same bytes three
        // times and makes MapLibre warn on duplicate ids.
        expect(spatial.length).toBe(3);

        const map = fakeMap();
        addMvtLayers(map, { ...OPTS, layers: spatial });

        expect(map.sources.size).toBe(1);
        expect(map.layers.size).toBeGreaterThanOrEqual(3);
    });

    it('points every source at the project and data version', () => {
        const map = fakeMap();
        addMvtLayers(map, { ...OPTS, layers: spatial });

        const src = [...map.sources.values()][0];
        const url = (src.tiles as string[])[0];
        expect(url).toContain('/tiles/silver/pg_spatial_features_by_project/');
        expect(url).toContain('project_id=p-1');
        expect(url).toContain('v=7');
        expect(url).toContain('{z}/{x}/{y}');
    });

    it('is idempotent — a second call adds nothing and does not throw', () => {
        // map.on('load') and a style change can both reach here, and MapLibre
        // throws on a duplicate id.
        const map = fakeMap();
        addMvtLayers(map, OPTS);
        const sourceCount = map.sources.size;
        const layerCount = map.layers.size;

        expect(() => addMvtLayers(map, OPTS)).not.toThrow();
        expect(map.sources.size).toBe(sourceCount);
        expect(map.layers.size).toBe(layerCount);
    });

    it('starts hidden unless the layer is switched on', () => {
        const map = fakeMap();
        const def = spatial[0];
        addMvtLayers(map, { ...OPTS, layers: [def] });

        const layout = map.layers.get(mvtLayerId(def))?.layout as { visibility: string };
        expect(layout.visibility).toBe('none');
    });

    it('honours an initial visible layer', () => {
        const map = fakeMap();
        const def = spatial[0];
        addMvtLayers(map, { ...OPTS, layers: [def], visibleLayers: { [def.id]: true } });

        const layout = map.layers.get(mvtLayerId(def))?.layout as { visibility: string };
        expect(layout.visibility).toBe('visible');
    });

    it('gives a fill definition its outline companion', () => {
        // A fill with no stroke reads as a wash; the outline is what makes an
        // imported polygon legible against the basemap.
        const fill = MVT_LAYERS.find((l) => l.outline) as MvtLayerDef;
        expect(fill).toBeTruthy();

        const map = fakeMap();
        addMvtLayers(map, { ...OPTS, layers: [fill] });

        expect(map.layers.has(mvtOutlineLayerId(fill))).toBe(true);
        expect(map.layers.get(mvtOutlineLayerId(fill))?.type).toBe('line');
    });

    it('spans the widest zoom window when a source is shared', () => {
        const map = fakeMap();
        addMvtLayers(map, { ...OPTS, layers: spatial });

        const src = [...map.sources.values()][0];
        expect(src.minzoom).toBe(Math.min(...spatial.map((d) => d.minzoom)));
        expect(src.maxzoom).toBe(Math.max(...spatial.map((d) => d.maxzoom)));
    });
});

describe('setMvtVisibility', () => {
    it('toggles without touching sources', () => {
        const map = fakeMap();
        addMvtLayers(map, OPTS);
        const before = map.sources.size;

        const def = spatial[0];
        setMvtVisibility(map, { [def.id]: true });

        expect(map.visibility[`${mvtLayerId(def)}.visibility`]).toBe('visible');
        expect(map.sources.size).toBe(before);
    });

    it('hides a layer absent from the visibility map', () => {
        const map = fakeMap();
        addMvtLayers(map, OPTS);
        const def = spatial[0];

        setMvtVisibility(map, {});

        expect(map.visibility[`${mvtLayerId(def)}.visibility`]).toBe('none');
    });

    it('moves an outline with its fill', () => {
        const fill = MVT_LAYERS.find((l) => l.outline) as MvtLayerDef;
        const map = fakeMap();
        addMvtLayers(map, { ...OPTS, layers: [fill] });

        setMvtVisibility(map, { [fill.id]: true }, [fill]);

        expect(map.visibility[`${mvtOutlineLayerId(fill)}.visibility`]).toBe('visible');
    });

    it('does nothing for a layer that was never added', () => {
        const map = fakeMap();
        expect(() => setMvtVisibility(map, { collars: true })).not.toThrow();
    });
});

describe('removeMvtLayers', () => {
    it('takes layers and sources back off', () => {
        const map = fakeMap();
        addMvtLayers(map, OPTS);

        removeMvtLayers(map);

        expect(map.layers.size).toBe(0);
        expect(map.sources.size).toBe(0);
    });

    it('removes layers before their shared source', () => {
        // MapLibre refuses to remove a source a layer still references, and
        // the outline shares its source with the fill.
        const map = fakeMap();
        const order: string[] = [];
        const tracked: MvtCapableMap = {
            ...map,
            removeLayer: (id) => {
                order.push(`layer:${id}`);
                map.layers.delete(id);
            },
            removeSource: (id) => {
                order.push(`source:${id}`);
                map.sources.delete(id);
            },
        };
        addMvtLayers(map, { ...OPTS, layers: spatial });
        removeMvtLayers(tracked, spatial);

        const firstSource = order.findIndex((o) => o.startsWith('source:'));
        const lastLayer = order.map((o) => o.startsWith('layer:')).lastIndexOf(true);
        expect(lastLayer).toBeLessThan(firstSource);
    });
});
