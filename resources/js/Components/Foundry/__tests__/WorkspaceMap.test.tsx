/**
 * WorkspaceMap against a fake MapLibre — the behaviours the 2026-09-29 audit
 * found broken, driven through the real component:
 *
 *   FE-3  the map is built with no positioned collars (extent / default view)
 *   FE-5  a new data_version re-keys the MVT tile URLs
 *   FE-6  a basemap switch re-applies layer visibility, filters and terrain
 *         to the NEW map instance
 *   FE-7  no synthetic due-south trace source
 *   FE-15 the collar popup links to the per-hole page
 *   GIS-5 the uncertainty-ring layer is added with the shared, valid paint
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
    layerHandlers: Record<string, Handler> = {};
    removed = false;
    dragPan = { enable: vi.fn(), disable: vi.fn() };
    constructor(opts: Record<string, unknown>) {
        this.opts = opts;
        FakeMap.instances.push(this);
    }
    addControl() {}
    on(event: string, a: unknown, b?: unknown) {
        if (typeof a === 'function') {
            (this.handlers[event] ??= []).push(a as Handler);
        } else {
            this.layerHandlers[`${event}:${a as string}`] = b as Handler;
        }
    }
    off() {}
    fire(event: string) {
        for (const h of this.handlers[event] ?? []) h();
    }
    addSource(id: string, s: Record<string, unknown>) {
        if (this.sources.has(id)) throw new Error(`dup source ${id}`);
        const src: Record<string, unknown> = { ...s, setData: vi.fn(), setTiles: vi.fn() };
        this.sources.set(id, src);
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
    default: {
        Map: FakeMap,
        NavigationControl: class {},
        ScaleControl: class {},
    },
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
    spatial_uncertainty_m: 25,
    georef_method: 'assumed',
};

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

describe('WorkspaceMap', () => {
    it('re-applies layer state to the new map after a basemap switch (FE-6)', async () => {
        const { rerender } = render(<WorkspaceMap {...baseProps()} />);
        const first = await loadedMap(0);
        expect(first.layout['collars-heatmap.visibility']).toBe('visible');
        expect(first.filters['collars-dot']).toEqual(['>=', ['get', 'ore_thickness_m'], 10]);
        expect(first.terrain).toMatchObject({ source: 'terrain-dem' });

        rerender(<WorkspaceMap {...baseProps({ basemap: 'positron' })} />);
        const second = await loadedMap(1);

        expect(first.removed).toBe(true);
        expect(second.layout['collars-heatmap.visibility']).toBe('visible');
        expect(second.filters['collars-dot']).toEqual(['>=', ['get', 'ore_thickness_m'], 10]);
        expect(second.terrain).toMatchObject({ source: 'terrain-dem' });
        // The real MVT traces follow the same toggle.
        expect(second.layout['mvt-traces.visibility']).toBe('visible');
    });

    it('builds the map with no positioned collars, from the project extent (FE-3)', async () => {
        render(<WorkspaceMap {...baseProps({ collars: [], projectExtent: [-106.2, 57.1, -105.8, 57.4] })} />);
        const m = await loadedMap(0);
        expect(m.opts.bounds).toEqual([-106.2, 57.1, -105.8, 57.4]);
        // Imported GIS layers are still attached.
        expect(m.getLayer('mvt-imported-polygons')).toBeDefined();
    });

    it('re-keys MVT tile URLs when data_version moves (FE-5)', async () => {
        const { rerender } = render(<WorkspaceMap {...baseProps()} />);
        const m = await loadedMap(0);
        const source = m.getSource('mvt-traces-source') as { tiles: string[]; setTiles: ReturnType<typeof vi.fn> };
        expect(source.tiles[0]).toContain('&v=3');

        rerender(<WorkspaceMap {...baseProps({ dataVersion: 5 })} />);
        await waitFor(() => expect(source.setTiles).toHaveBeenCalledWith([expect.stringContaining('&v=5')]));
    });

    it('adds no synthetic trace source, and the uncertainty ring uses the shared paint (FE-7, GIS-5)', async () => {
        render(<WorkspaceMap {...baseProps()} />);
        const m = await loadedMap(0);
        expect(m.getSource('collar-traces')).toBeUndefined();
        const ring = m.getLayer('uncertainty-rings') as { paint: Record<string, unknown> };
        expect((ring.paint['circle-radius'] as unknown[])[0]).toBe('interpolate');
        expect(m.getSource('terrain-dem')).toMatchObject({ url: 'https://dem.example.test/tilejson.json' });
    });

    it('links the collar popup to the per-hole page (FE-15)', async () => {
        render(<WorkspaceMap {...baseProps({ activeHole: collar })} />);
        await loadedMap(0);
        const link = screen.getByRole('link', { name: /open hole page/i });
        expect(link).toHaveAttribute('href', `/projects/red-star/holes/${collar.collar_id}/detail`);
        fireEvent.click(screen.getByText(/view in logs/i));
    });
});
