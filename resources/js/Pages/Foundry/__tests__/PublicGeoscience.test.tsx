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
import { render, act, cleanup } from '@testing-library/react';
import type { ReactNode } from 'react';

const { handlers, state } = vi.hoisted(() => ({
    handlers: {} as Record<string, Array<(...args: unknown[]) => void>>,
    state: { removed: false, layers: new Set<string>(), sources: new Set<string>(), container: null as HTMLElement | null },
}));

vi.mock('maplibre-gl', () => {
    const guard = () => {
        if (state.removed) throw new TypeError("Cannot read properties of undefined (reading 'getLayer')");
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function MapMock(this: any, opts: { container: HTMLElement }) {
        state.container = opts.container;
        this.addControl = vi.fn();
        this.on = (event: string, fn: (...args: unknown[]) => void) => {
            (handlers[event] ??= []).push(fn);
        };
        this.off = vi.fn();
        this.remove = () => {
            state.removed = true;
        };
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
        this.queryRenderedFeatures = () => [];
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
    return { default: mod, ...mod };
});
vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}));
vi.mock('@/Layouts/AppLayout', () => ({ default: ({ children }: { children: ReactNode }) => <div>{children}</div> }));
vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    usePage: () => ({ props: { auth: { user: { is_admin: false } } } }),
}));
vi.mock('@/Components/PublicGeoscience/PublicGeoSyncControls', () => ({ default: () => null }));
vi.mock('@/lib/basemap', () => ({ useBasemapStyleUrl: () => 'https://example.test/style.json' }));

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

beforeEach(() => {
    state.removed = false;
    state.layers.clear();
    state.sources.clear();
    state.container = null;
    for (const key of Object.keys(handlers)) delete handlers[key];
    vi.stubGlobal(
        'fetch',
        vi.fn(async () => new Response(JSON.stringify(featureCollection), { status: 200, headers: { 'Content-Type': 'application/json' } })),
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
});
