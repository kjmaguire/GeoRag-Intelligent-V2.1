/**
 * MapView public-geoscience overlay, MVT hover popup and tile-failure toast.
 *
 *   - the public-geoscience hover popup HTML-escapes feature properties
 *     (a label containing `<img onerror>` must not reach `Popup.setHTML` raw)
 *   - the overlay refetches (debounced 300 ms) after the map stops moving
 *   - the MVT hover popup is removed when the pointer leaves every feature
 *   - the tile-failure toast names the layer, not the `/tiles/silver/` path
 *
 * MapLibre needs WebGL, which jsdom lacks, so maplibre-gl is mocked at module
 * level (same approach as MapView.layerToggle.test.tsx).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

const {
    mockOn,
    mockOff,
    mockGetLayer,
    mockQueryRenderedFeatures,
    mockSetHTML,
    mockPopupRemove,
    mockPopupInstances,
} = vi.hoisted(() => ({
    mockOn: vi.fn(),
    mockOff: vi.fn(),
    mockGetLayer: vi.fn().mockReturnValue(true),
    mockQueryRenderedFeatures: vi.fn().mockReturnValue([]),
    mockSetHTML: vi.fn(),
    mockPopupRemove: vi.fn(),
    mockPopupInstances: [] as Array<{ options: Record<string, unknown> }>,
}));

vi.mock('maplibre-gl', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function MapMock(this: any) {
        this.addControl = vi.fn();
        this.on = mockOn;
        this.off = mockOff;
        this.remove = vi.fn();
        this.getSource = vi.fn().mockReturnValue(null);
        this.addSource = vi.fn();
        this.getLayer = mockGetLayer;
        this.addLayer = vi.fn();
        this.setLayoutProperty = vi.fn();
        this.setFilter = vi.fn();
        this.setTerrain = vi.fn();
        this.getZoom = vi.fn().mockReturnValue(5);
        this.getBounds = vi.fn().mockReturnValue({
            getWest: () => -110, getSouth: () => 50, getEast: () => -100, getNorth: () => 56,
        });
        this.getStyle = vi.fn().mockReturnValue({ layers: [] });
        this.getCanvas = vi.fn().mockReturnValue({ style: {} });
        this.fitBounds = vi.fn();
        this.panTo = vi.fn();
        this.easeTo = vi.fn();
        this.flyTo = vi.fn();
        this.removeLayer = vi.fn();
        this.removeSource = vi.fn();
        this.setPaintProperty = vi.fn();
        this.queryRenderedFeatures = mockQueryRenderedFeatures;
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function Ctrl(this: any) { void this; }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function MarkerMock(this: any) {
        this.setLngLat = vi.fn().mockReturnThis();
        this.addTo = vi.fn().mockReturnThis();
        this.remove = vi.fn();
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function PopupMock(this: any, options: Record<string, unknown> = {}) {
        this.setLngLat = vi.fn().mockReturnThis();
        this.setHTML = mockSetHTML.mockReturnThis();
        this.addTo = vi.fn().mockReturnThis();
        this.remove = mockPopupRemove;
        this.options = options;
        mockPopupInstances.push(this);
    }
    return {
        Map: MapMock,
        NavigationControl: Ctrl,
        FullscreenControl: Ctrl,
        ScaleControl: Ctrl,
        Marker: MarkerMock,
        Popup: PopupMock,
    };
});

vi.mock('@inertiajs/react', () => ({
    usePage: () => ({
        props: {
            workspace: { id: 'ws-1', name: 'Test', data_version: 1 },
            auth: { user: null },
            flash: { success: null, error: null },
            app: { env: 'test', debug: false },
        },
    }),
}));

import MapView, { tileSourceLabel } from '../MapView';
import { MVT_LAYERS, mvtSourceId } from '../../lib/mvtLayers';

type Handler = (...args: unknown[]) => void;

/** Handlers registered via map.on(event, fn) (no layer id). */
function mapHandlers(event: string): Handler[] {
    return mockOn.mock.calls
        .filter(([name, second]) => name === event && typeof second === 'function')
        .map(([, fn]) => fn as Handler);
}

/** Handlers registered via map.on(event, layerId, fn). */
function layerHandlers(event: string, layerId: string): Handler[] {
    return mockOn.mock.calls
        .filter(([name, id]) => name === event && id === layerId)
        .map(([, , fn]) => fn as Handler);
}

function triggerMapLoad() {
    for (const cb of mapHandlers('load')) act(() => { cb(); });
}

function enablePublicGeo() {
    fireEvent.click(document.getElementById('layer-toggle-public-geoscience') as HTMLInputElement);
}

const emptyOverlay = { type: 'FeatureCollection', features: [], feature_count: 0 };
let fetchSpy: ReturnType<typeof vi.spyOn>;

const publicGeoFetches = () =>
    fetchSpy.mock.calls.filter(([url]) => String(url).startsWith('/api/v1/public-geoscience/map'));

beforeEach(() => {
    vi.clearAllMocks();
    mockPopupInstances.length = 0;
    mockGetLayer.mockReturnValue(true);
    mockQueryRenderedFeatures.mockReturnValue([]);
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(
        JSON.stringify(emptyOverlay),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
    ));
});

afterEach(() => {
    fetchSpy.mockRestore();
});

describe('MapView public-geoscience hover popup', () => {
    it('escapes a label containing markup before it reaches Popup.setHTML', async () => {
        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();
        await screen.findByRole('region', { name: 'Map layer toggles' });
        enablePublicGeo();
        await waitFor(() => expect(layerHandlers('mousemove', 'public-geoscience-circle')).toHaveLength(1));

        const payload = '<img src=x onerror=alert(1)>';
        mockQueryRenderedFeatures.mockReturnValue([{
            properties: {
                layer: 'mine',
                label: payload,
                jurisdiction_code: '<b>CA-SK</b>',
                cluster: false,
            },
        }]);
        const [onMove] = layerHandlers('mousemove', 'public-geoscience-circle');
        act(() => { onMove({ point: { x: 1, y: 1 }, lngLat: { lng: -105, lat: 55 } }); });

        const html = mockSetHTML.mock.calls.at(-1)?.[0] as string;
        expect(html).not.toContain('<img');
        expect(html).not.toContain('<b>');
        expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
        expect(html).toContain('&lt;b&gt;CA-SK&lt;/b&gt;');
    });

    it('escapes a cluster popup too', async () => {
        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();
        await screen.findByRole('region', { name: 'Map layer toggles' });
        enablePublicGeo();
        await waitFor(() => expect(layerHandlers('mousemove', 'public-geoscience-circle')).toHaveLength(1));

        mockQueryRenderedFeatures.mockReturnValue([{
            properties: { layer: '<script>x</script>', cluster: true, point_count: 1234 },
        }]);
        const [onMove] = layerHandlers('mousemove', 'public-geoscience-circle');
        act(() => { onMove({ point: { x: 1, y: 1 }, lngLat: { lng: -105, lat: 55 } }); });

        const html = mockSetHTML.mock.calls.at(-1)?.[0] as string;
        expect(html).not.toContain('<script>');
        expect(html).toContain('&lt;script&gt;x&lt;/script&gt;');
        expect(html).toContain('records');
    });
});

describe('MapView public-geoscience overlay refetch', () => {
    it('refetches once, 300 ms after the map stops moving, and unsubscribes on disable', async () => {
        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();
        await screen.findByRole('region', { name: 'Map layer toggles' });
        enablePublicGeo();
        await waitFor(() => expect(publicGeoFetches()).toHaveLength(1));

        const [onMoveEnd] = mapHandlers('moveend');
        expect(onMoveEnd).toBeDefined();

        // A burst of moveend events is one refetch, not three, and not instant.
        act(() => { onMoveEnd(); onMoveEnd(); onMoveEnd(); });
        expect(publicGeoFetches()).toHaveLength(1);
        await waitFor(() => expect(publicGeoFetches()).toHaveLength(2), { timeout: 1500 });
        await new Promise((resolve) => setTimeout(resolve, 450));
        expect(publicGeoFetches()).toHaveLength(2);

        fireEvent.click(document.getElementById('layer-toggle-public-geoscience') as HTMLInputElement);
        expect(mockOff.mock.calls.some(([name, fn]) => name === 'moveend' && fn === onMoveEnd)).toBe(true);
    });
});

describe('MapView MVT hover popup', () => {
    it('removes the hover popup when the pointer moves off every feature', async () => {
        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();
        await screen.findByRole('region', { name: 'Map layer toggles' });
        await waitFor(() => expect(mapHandlers('mousemove').length).toBeGreaterThan(0));

        const onMove = mapHandlers('mousemove').at(-1) as Handler;
        mockQueryRenderedFeatures.mockReturnValue([
            { properties: { hole_id: 'DH-1' }, sourceLayer: 'collars' },
        ]);
        act(() => { onMove({ point: { x: 1, y: 1 }, lngLat: { lng: -105, lat: 55 } }); });
        expect(mockPopupInstances).toHaveLength(1);
        mockPopupRemove.mockClear();

        mockQueryRenderedFeatures.mockReturnValue([]);
        act(() => { onMove({ point: { x: 50, y: 50 }, lngLat: { lng: -104, lat: 55 } }); });
        expect(mockPopupRemove).toHaveBeenCalledTimes(1);

        // Hovering the same feature again opens a fresh popup (the stale
        // popup reference was cleared, so the debounce does not swallow it).
        mockQueryRenderedFeatures.mockReturnValue([
            { properties: { hole_id: 'DH-1' }, sourceLayer: 'collars' },
        ]);
        act(() => { onMove({ point: { x: 1, y: 1 }, lngLat: { lng: -105, lat: 55 } }); });
        expect(mockPopupInstances).toHaveLength(2);
    });
});

describe('MapView tile-failure toast', () => {
    it('maps a source id to the registry layer label', () => {
        const layer = MVT_LAYERS[0];
        expect(tileSourceLabel(mvtSourceId(layer))).toBe(layer.label);
        expect(tileSourceLabel('openmaptiles')).toBeNull();
    });

    it('names the layer and never prints the tile path', async () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();
        await screen.findByRole('region', { name: 'Map layer toggles' });
        await waitFor(() => expect(mapHandlers('error').length).toBeGreaterThan(0));

        const layer = MVT_LAYERS[0];
        const onError = mapHandlers('error').at(-1) as Handler;
        for (let i = 0; i < 3; i += 1) {
            act(() => { onError({ sourceId: mvtSourceId(layer), error: { status: 500 } }); });
        }

        const toast = await screen.findByRole('alert');
        expect(toast.textContent).toContain(`Map tiles for ${layer.label} failed to load`);
        expect(toast.textContent).not.toContain('/tiles/');
        expect(toast.textContent).not.toContain('[source');
        warn.mockRestore();
    });
});
