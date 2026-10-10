/**
 * PublicGeoscience page — the two defects the first AWS session hit
 * (2026-09-29): an empty map under a correct record count, and "Something
 * went wrong" on every navigation away.
 *
 * MapLibre needs WebGL, which jsdom lacks, so maplibre-gl is mocked. The mock
 * reproduces the one behaviour that matters here: after remove(), style
 * lookups throw, the way the real map does once its style is destroyed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup, fireEvent, screen } from '@testing-library/react';
import type { ReactNode } from 'react';

const { handlers, state } = vi.hoisted(() => ({
    handlers: {} as Record<string, Array<(...args: unknown[]) => void>>,
    state: {
        removed: false,
        maps: 0,
        layers: new Set<string>(),
        sources: new Set<string>(),
        container: null as HTMLElement | null,
        rendered: [] as unknown[],
        // Make the next Map construction throw, as maplibre-gl does with no WebGL 2.
        failToStart: false,
    },
}));

vi.mock('maplibre-gl', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function MapMock(this: any, opts: { container: HTMLElement }) {
        if (state.failToStart) throw new Error('Failed to initialize WebGL');
        // Each map has its own removed flag: a basemap switch builds a new
        // map while the old one's layer cleanups are still pending.
        let removed = false;
        const guard = () => {
            if (removed) throw new TypeError("Cannot read properties of undefined (reading 'getLayer')");
        };
        state.maps += 1;
        state.removed = false;
        state.layers.clear();
        state.sources.clear();
        state.container = opts.container;
        this.addControl = vi.fn();
        this.on = (event: string, fn: (...args: unknown[]) => void) => {
            (handlers[event] ??= []).push(fn);
        };
        this.off = vi.fn();
        this.remove = () => {
            removed = true;
            state.removed = true;
        };
        this.getCenter = () => ({ lng: -105, lat: 55 });
        this.setFilter = () => guard();
        this.getBounds = () => ({ getWest: () => -110, getSouth: () => 49, getEast: () => -101, getNorth: () => 60 });
        this.getZoom = () => 4;
        this.getCanvas = () => ({ style: {} });
        this.getLayer = (id: string) => {
            guard();
            return state.layers.has(id) ? { id } : undefined;
        };
        this.getSource = (id: string) => {
            guard();
            return state.sources.has(id) ? { setData: vi.fn() } : undefined;
        };
        this.addSource = (id: string) => {
            guard();
            state.sources.add(id);
        };
        this.addLayer = (layer: { id: string }) => {
            guard();
            state.layers.add(layer.id);
        };
        this.removeLayer = (id: string) => {
            guard();
            state.layers.delete(id);
        };
        this.removeSource = (id: string) => {
            guard();
            state.sources.delete(id);
        };
        this.queryRenderedFeatures = () => state.rendered;
        this.easeTo = vi.fn();
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function Control(this: any) {}
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function PopupMock(this: any) {
        this.setLngLat = () => this;
        this.setHTML = () => this;
        this.addTo = () => this;
        this.remove = vi.fn();
    }
    const mod = { Map: MapMock, NavigationControl: Control, ScaleControl: Control, Popup: PopupMock };
    // maplibre-gl 6 is ESM-only: named exports, no default export.
    return { ...mod };
});
vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}));
vi.mock('@/Layouts/AppLayout', () => ({ default: ({ children }: { children: ReactNode }) => <div>{children}</div> }));
vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    usePage: () => ({ props: { auth: { user: { is_admin: false } } } }),
}));
vi.mock('@/Components/PublicGeoscience/PublicGeoSyncControls', () => ({ default: () => null }));
vi.mock('@/lib/basemap', () => ({
    BASEMAP_OPTIONS: [
        { id: 'dark_matter', label: 'Dark' },
        { id: 'positron', label: 'Light (Positron)' },
    ],
    useBasemapStyleSpec: (id: string) => `https://example.test/${id}.json`,
}));

import PublicGeoscience from '../PublicGeoscience';

const featureCollection = {
    type: 'FeatureCollection',
    features: [
        {
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [-105, 55] },
            properties: { cluster: false, layer: 'mine', label: 'Test mine', jurisdiction_code: 'CA-SK' },
        },
    ],
    total_in_view: 1,
    feature_count: 1,
    modes: { mine: 'points' },
};

const drillholeRecord = {
    title: 'Drillhole PLS-20-001',
    jurisdiction: { code: 'CA-SK', name: 'Saskatchewan', authority: null },
    source: {
        source_id: 'CA-SK-DRILLHOLE',
        name: 'Saskatchewan Minerals & Quaternary Drillhole Compilation',
        service_url: null,
    },
    license: {
        summary: 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0',
        url: 'https://example.test/licence.pdf',
    },
    refresh: { last_refreshed_at: null },
    references_summary: { count: 0, documents: [] },
    entity: {
        drillhole_id: 'GOS-9001',
        drillhole_name: 'PLS-20-001',
        company: 'TestDrill Inc.',
        project_name: 'Patterson Lake South',
        total_length_m: '350.5',
        inclination_deg: '-70.00',
        azimuth_deg: '135.00',
        core_availability: 'available',
        stratigraphic_depths: { base_of_quaternary: { depth_m: 42.1, elevation_m: 480.2 } },
    },
};

beforeEach(() => {
    state.removed = false;
    state.maps = 0;
    state.rendered = [];
    state.layers.clear();
    state.sources.clear();
    state.container = null;
    state.failToStart = false;
    for (const key of Object.keys(handlers)) delete handlers[key];
    vi.stubGlobal(
        'fetch',
        vi.fn(
            async (url: string) =>
                new Response(
                    JSON.stringify(String(url).includes('/citations/resolve') ? drillholeRecord : featureCollection),
                    {
                        status: 200,
                        headers: { 'Content-Type': 'application/json' },
                    },
                ),
        ),
    );
});

afterEach(() => {
    vi.unstubAllGlobals();
});

async function mountAndLoad() {
    const view = render(<PublicGeoscience />);
    await act(async () => {
        for (const fn of handlers.load ?? []) fn();
    });
    // Let the fetch resolve and the layer effect run.
    await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
    });
    return view;
}

describe('PublicGeoscience', () => {
    it('keeps the page and says why when the map cannot start (no WebGL 2)', async () => {
        state.failToStart = true;
        vi.spyOn(console, 'error').mockImplementation(() => {});

        render(<PublicGeoscience />);

        // Thrown into the root error boundary, this used to take the page.
        expect(screen.getByTestId('map-start-failure')).toHaveTextContent(/needs WebGL 2/);
        expect(state.maps).toBe(0);
    });

    it('sizes the map element with an inline style, not a Tailwind position utility', async () => {
        await mountAndLoad();
        const el = state.container!;
        // maplibre-gl.css forces `.maplibregl-map { position: relative }`
        // unlayered, which overrides Tailwind v4's `absolute`. Only an inline
        // size survives that.
        expect(el.style.width).toBe('100%');
        expect(el.style.height).toBe('100%');
        expect(el.className).not.toContain('absolute');
        expect(el.parentElement?.className).toContain('absolute');
    });

    it('draws the point layers once data arrives', async () => {
        await mountAndLoad();
        expect(state.layers.size).toBeGreaterThan(0);
    });

    it('unmounts without throwing after the map is removed', async () => {
        await mountAndLoad();
        expect(state.layers.size).toBeGreaterThan(0);
        // Before the fix the layer cleanup called getLayer() on the removed
        // map and the throw reached the error boundary.
        expect(() => cleanup()).not.toThrow();
        expect(state.removed).toBe(true);
    });

    it('opens the hole card with the full record when a drillhole is clicked', async () => {
        await mountAndLoad();
        state.rendered = [
            {
                geometry: { type: 'Point', coordinates: [-109.1, 57.6] },
                properties: {
                    cluster: false,
                    layer: 'drillhole_collar',
                    id: 'pg-uuid-1',
                    source_id: 'CA-SK-DRILLHOLE',
                    label: 'PLS-20-001',
                    jurisdiction_code: 'CA-SK',
                },
            },
        ];
        await act(async () => {
            for (const fn of handlers.click ?? []) fn({ point: { x: 10, y: 10 }, lngLat: { lng: -109.1, lat: 57.6 } });
            await new Promise((r) => setTimeout(r, 0));
        });

        const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>;
        const resolveUrl = fetchMock.mock.calls.map((c) => String(c[0])).find((u) => u.includes('/citations/resolve'));
        expect(resolveUrl).toContain(encodeURIComponent('pg_drillhole_collar:CA-SK-DRILLHOLE:pg_id=pg-uuid-1'));

        const card = screen.getByRole('dialog');
        expect(card.textContent).toContain('Public drillhole');
        expect(card.textContent).toContain('PLS-20-001');
        expect(card.textContent).toContain('350.5 m');
        expect(card.textContent).toContain('-70° / 135°');
        expect(card.textContent).toContain('Base of Quaternary');

        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('zooms into a cluster rather than opening a card', async () => {
        await mountAndLoad();
        state.rendered = [
            {
                geometry: { type: 'Point', coordinates: [-105, 55] },
                properties: { cluster: true, layer: 'drillhole_collar', point_count: 120 },
            },
        ];
        await act(async () => {
            for (const fn of handlers.click ?? []) fn({ point: { x: 10, y: 10 }, lngLat: { lng: -105, lat: 55 } });
        });
        expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('switches basemap by rebuilding the map without throwing', async () => {
        await mountAndLoad();
        expect(state.maps).toBe(1);
        // The old map's layer cleanups run after the new map exists — the
        // case a single shared "removed" flag got wrong.
        expect(() =>
            fireEvent.change(screen.getByRole('combobox', { name: 'Basemap' }), { target: { value: 'positron' } }),
        ).not.toThrow();
        expect(state.maps).toBe(2);
        expect(() => cleanup()).not.toThrow();
    });
});
