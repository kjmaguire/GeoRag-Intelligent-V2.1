/**
 * WorkspaceMap builds its MapLibre instance ONCE per project. A basemap
 * switch swaps the style on that instance (`setStyle`) and re-adds the
 * sources and layers on `style.load`; a new `collars` prop updates the
 * GeoJSON source in place. Neither may rebuild the map, because a rebuild
 * resets the camera.
 */
import { act, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';

type Handler = (...args: unknown[]) => void;

class FakeMap {
    static instances: FakeMap[] = [];
    opts: Record<string, unknown>;
    sources = new Map<string, Record<string, unknown>>();
    layers = new Map<string, Record<string, unknown>>();
    layout: Record<string, unknown> = {};
    filters: Record<string, unknown> = {};
    terrain: unknown = undefined;
    handlers: Record<string, Handler[]> = {};
    onceHandlers: Record<string, Handler[]> = {};
    setStyleCalls: Array<{ style: unknown; options: unknown }> = [];
    removed = false;
    dragPan = { enable: vi.fn(), disable: vi.fn() };
    constructor(opts: Record<string, unknown>) {
        this.opts = opts;
        FakeMap.instances.push(this);
    }
    addControl() {}
    on(event: string, a: unknown) {
        if (typeof a === 'function') (this.handlers[event] ??= []).push(a as Handler);
    }
    once(event: string, cb: Handler) {
        (this.onceHandlers[event] ??= []).push(cb);
    }
    off() {}
    fire(event: string) {
        for (const h of this.handlers[event] ?? []) h();
        const once = this.onceHandlers[event] ?? [];
        this.onceHandlers[event] = [];
        for (const h of once) h();
    }
    setStyle(style: unknown, options: unknown) {
        // Like MapLibre: a new style discards every runtime source and layer
        // (and, with them, terrain and per-layer paint state).
        this.setStyleCalls.push({ style, options });
        this.sources.clear();
        this.layers.clear();
        this.terrain = undefined;
    }
    addSource(id: string, s: Record<string, unknown>) {
        if (this.sources.has(id)) throw new Error(`dup source ${id}`);
        this.sources.set(id, { ...s, setData: vi.fn(), setTiles: vi.fn() });
    }
    getSource(id: string) {
        return this.sources.get(id);
    }
    addLayer(l: Record<string, unknown>) {
        if (this.layers.has(l.id as string)) throw new Error(`dup layer ${String(l.id)}`);
        this.layers.set(l.id as string, l);
    }
    getLayer(id: string) {
        return this.layers.get(id);
    }
    setLayoutProperty(id: string, name: string, v: unknown) {
        this.layout[`${id}.${name}`] = v;
    }
    setFilter(id: string, f: unknown) {
        this.filters[id] = f;
    }
    setTerrain(t: unknown) {
        this.terrain = t;
    }
    getCanvas() {
        return { style: {} as Record<string, string> };
    }
    queryRenderedFeatures() {
        return [];
    }
    remove() {
        this.removed = true;
    }
}

vi.mock('maplibre-gl', () => ({
    Map: FakeMap,
    NavigationControl: class {},
    ScaleControl: class {},
}));

vi.mock('@inertiajs/react', () => ({
    usePage: () => ({ props: { basemap_dem: 'https://dem.example.test/tilejson.json' } }),
    router: { get: vi.fn(), visit: vi.fn() },
    Link: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
        <a href={href} {...rest}>{children}</a>
    ),
}));

import { WorkspaceMap, type MapCollar } from '../WorkspaceMap';

const collar: MapCollar = {
    collar_id: '11111111-2222-3333-4444-555555555555',
    hole_id: 'RS-001',
    hole_id_canonical: 'RS-001',
    total_depth: 300,
    lat: 58.1,
    lng: -105.2,
    ore_bands: 1,
    ore_thickness_m: 12,
};
const secondCollar: MapCollar = { ...collar, collar_id: '99999999-2222-3333-4444-555555555555', hole_id: 'RS-002', hole_id_canonical: 'RS-002', lat: 58.2 };

function baseProps(over: Partial<ComponentProps<typeof WorkspaceMap>> = {}): ComponentProps<typeof WorkspaceMap> {
    return {
        collars: [collar],
        projectSlug: 'red-star',
        projectId: 'p-uuid',
        projectInfo: { project_name: 'Red Star', company: null, commodity: null, region: null, crs_epsg: 26913 },
        projectSummary: { total_drilled_m: 300, mean_td_m: 300, ore_hole_count: 1, total_ore_thickness_m: 12, mean_u3o8_pct: null },
        visibleLayers: { collars: true, ore_heatmap: true, tier_10: true, traces: true },
        projectAoi: null,
        activeHole: null,
        setActiveHole: vi.fn(),
        compareSet: [],
        onToggleCompare: vi.fn(),
        onOpenCompare: vi.fn(),
        onClearCompare: vi.fn(),
        basemap: 'dark_matter',
        onBasemapChange: vi.fn(),
        terrainOn: true,
        onTerrainChange: vi.fn(),
        activeTool: 'pan',
        onToolChange: vi.fn(),
        dataVersion: 3,
        ...over,
    };
}

async function loadedMap(index: number): Promise<FakeMap> {
    await waitFor(() => expect(FakeMap.instances.length).toBeGreaterThan(index));
    const m = FakeMap.instances[index];
    await act(async () => {
        m.fire('load');
    });
    return m;
}

beforeEach(() => {
    FakeMap.instances = [];
});
afterEach(() => {
    vi.clearAllMocks();
});

describe('WorkspaceMap style switch and collar updates', () => {
    it('swaps the style on the SAME map and re-applies layers, filters and terrain after style.load', async () => {
        const { rerender } = render(<WorkspaceMap {...baseProps()} />);
        const map = await loadedMap(0);
        expect(map.layout['collars-heatmap.visibility']).toBe('visible');
        expect(map.terrain).toMatchObject({ source: 'terrain-dem' });

        rerender(<WorkspaceMap {...baseProps({ basemap: 'positron' })} />);

        // One instance, never rebuilt: the camera is whatever the user left it.
        await waitFor(() => expect(map.setStyleCalls).toHaveLength(1));
        expect(FakeMap.instances).toHaveLength(1);
        expect(map.removed).toBe(false);
        expect(map.setStyleCalls[0].options).toEqual({ diff: false });
        // The style swap wiped the runtime layers; terrain is not re-applied yet.
        expect(map.getLayer('collars-dot')).toBeUndefined();
        expect(map.terrain).toBeUndefined();

        await act(async () => {
            map.fire('style.load');
        });

        // Every layer id is back, and the state effects re-ran against it.
        for (const id of ['collars-heatmap', 'collars-halo', 'collars-dot', 'cluster-circles', 'cluster-count',
            'collars-compare-ring', 'collars-label', 'uncertainty-rings', 'spider-lines', 'spider-halo',
            'spider-dot', 'spider-label', 'mvt-traces']) {
            expect(map.getLayer(id), id).toBeDefined();
        }
        expect(map.getSource('terrain-dem')).toBeDefined();
        await waitFor(() => expect(map.terrain).toMatchObject({ source: 'terrain-dem' }));
        expect(map.layout['collars-heatmap.visibility']).toBe('visible');
        expect(map.filters['collars-dot']).toEqual(['>=', ['get', 'ore_thickness_m'], 10]);
        expect(map.layout['mvt-traces.visibility']).toBe('visible');
        expect(FakeMap.instances).toHaveLength(1);
    });

    it('does not touch the map for a re-render with an unchanged basemap', async () => {
        const { rerender } = render(<WorkspaceMap {...baseProps()} />);
        const map = await loadedMap(0);
        rerender(<WorkspaceMap {...baseProps({ activeTool: 'measure' })} />);
        expect(map.setStyleCalls).toHaveLength(0);
        expect(FakeMap.instances).toHaveLength(1);
    });

    it('updates the collars source in place when the collars prop changes', async () => {
        const { rerender } = render(<WorkspaceMap {...baseProps()} />);
        const map = await loadedMap(0);
        const source = map.getSource('collars') as { setData: ReturnType<typeof vi.fn> };
        // The initial load wrote the source itself; no redundant setData.
        expect(source.setData).not.toHaveBeenCalled();

        rerender(<WorkspaceMap {...baseProps({ collars: [collar, secondCollar] })} />);

        await waitFor(() => expect(source.setData).toHaveBeenCalledTimes(1));
        const data = source.setData.mock.calls[0][0] as { features: Array<{ properties: { hole_id: string } }> };
        expect(data.features.map((f) => f.properties.hole_id)).toEqual(['RS-001', 'RS-002']);
        expect(FakeMap.instances).toHaveLength(1);
        expect(map.removed).toBe(false);
    });

    it('seeds the re-added collars source from the latest collars after a style switch', async () => {
        const { rerender } = render(<WorkspaceMap {...baseProps()} />);
        const map = await loadedMap(0);

        rerender(<WorkspaceMap {...baseProps({ basemap: 'positron', collars: [collar, secondCollar] })} />);
        await act(async () => {
            map.fire('style.load');
        });

        const source = map.getSource('collars') as { data: { features: unknown[] }; setData: ReturnType<typeof vi.fn> };
        expect(source.data.features).toHaveLength(2);
        expect(source.setData).not.toHaveBeenCalled();
    });

    it('still builds a fresh map when the project changes', async () => {
        const { rerender } = render(<WorkspaceMap {...baseProps()} />);
        const first = await loadedMap(0);
        rerender(<WorkspaceMap {...baseProps({ projectSlug: 'blue-moon' })} />);
        await loadedMap(1);
        expect(first.removed).toBe(true);
        expect(FakeMap.instances).toHaveLength(2);
    });
});
