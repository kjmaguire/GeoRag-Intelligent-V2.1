/**
 * MapView layer toggle tests — Module 8 Chunk 8.7 (Deliverable C).
 *
 * Tests the silver layer toggle panel:
 *   - All 7 layers appear with correct labels
 *   - Toggling a layer calls setLayoutProperty on the correct MapLibre layer ids
 *   - Layers with outline (boundaries/formations/seismic) toggle BOTH fill + outline
 *   - Default visibility state matches MVT_DEFAULT_VISIBILITY
 *   - Panel has correct ARIA region role
 *
 * MapLibre's Map constructor requires WebGL, which jsdom lacks. We mock
 * maplibre-gl at the module level so the component can mount without a canvas.
 * The setLayoutProperty spy lets us assert toggle behaviour.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MVT_LAYERS, MVT_DEFAULT_VISIBILITY } from '../../lib/mvtLayers';

// ── MapLibre mock ──────────────────────────────────────────────────────────
// vi.mock() is hoisted to the top of the file by Vitest's transformer, so
// mock variable references in the factory must be declared via vi.hoisted()
// to avoid "Cannot access before initialization" TDZ errors.
const {
    mockSetLayoutProperty,
    mockGetLayer,
    mockGetSource,
    mockAddSource,
    mockAddLayer,
    mockAddControl,
    mockOn,
    mockOff,
    mockRemove,
    mockSetFilter,
    mockSetTerrain,
    mockGetZoom,
    mockGetStyle,
    mockGetCanvas,
} = vi.hoisted(() => ({
    mockSetLayoutProperty: vi.fn(),
    mockGetLayer:          vi.fn().mockReturnValue(true),
    mockGetSource:         vi.fn().mockReturnValue(null),
    mockAddSource:         vi.fn(),
    mockAddLayer:          vi.fn(),
    mockAddControl:        vi.fn(),
    mockOn:                vi.fn(),
    mockOff:               vi.fn(),
    mockRemove:            vi.fn(),
    mockSetFilter:         vi.fn(),
    mockSetTerrain:        vi.fn(),
    mockGetZoom:           vi.fn().mockReturnValue(5),
    mockGetStyle:          vi.fn().mockReturnValue({ layers: [] }),
    mockGetCanvas:         vi.fn().mockReturnValue({ style: {} }),
}));

vi.mock('maplibre-gl', () => {
    // All MapLibre classes are called with `new`. Using regular `function`
    // declarations ensures they work as constructors in vitest/jsdom.

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function MapMock(this: any) {
        this.addControl        = mockAddControl;
        this.on                = mockOn;
        this.off               = mockOff;
        this.remove            = mockRemove;
        this.getSource         = mockGetSource;
        this.addSource         = mockAddSource;
        this.getLayer          = mockGetLayer;
        this.addLayer          = mockAddLayer;
        this.setLayoutProperty = mockSetLayoutProperty;
        this.setFilter         = mockSetFilter;
        this.setTerrain        = mockSetTerrain;
        this.getZoom           = mockGetZoom;
        this.getStyle          = mockGetStyle;
        this.getCanvas         = mockGetCanvas;
        this.fitBounds         = vi.fn();
        this.panTo             = vi.fn();
        this.easeTo            = vi.fn();
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function NavCtrl(this: any) { void this; }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function FullscreenCtrl(this: any) { void this; }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function ScaleCtrl(this: any) { void this; }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function MarkerMock(this: any) {
        this.setLngLat = vi.fn().mockReturnThis();
        this.addTo     = vi.fn().mockReturnThis();
        this.remove    = vi.fn();
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function PopupMock(this: any) {
        this.setLngLat = vi.fn().mockReturnThis();
        this.setHTML   = vi.fn().mockReturnThis();
        this.addTo     = vi.fn().mockReturnThis();
        this.remove    = vi.fn();
        this.options   = {};
    }

    return {
        default: {
            Map:               MapMock,
            NavigationControl: NavCtrl,
            FullscreenControl: FullscreenCtrl,
            ScaleControl:      ScaleCtrl,
            Marker:            MarkerMock,
            Popup:             PopupMock,
        },
    };
});

// ── Inertia mock ───────────────────────────────────────────────────────────
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

// Import MapView AFTER mocks are registered
import MapView from '../MapView';

// ── Helper: simulate 'load' event to set mapReady = true ──────────────────
// MapView only renders the layer panel after mapReady is true (via useEffect).
// We trigger the 'load' callback that MapLibre fires after map initialisation.
function triggerMapLoad() {
    // Find the 'load' event listener registered via map.on('load', cb)
    const loadCall = mockOn.mock.calls.find(([event]) => event === 'load');
    if (loadCall) {
        const [, cb] = loadCall;
        cb();
    }
}

// ── Test suite ─────────────────────────────────────────────────────────────

describe('MapView layer toggle panel — Deliverable C', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        // Make getLayer return true so layers "exist" for setLayoutProperty calls
        mockGetLayer.mockReturnValue(true);
        mockGetSource.mockReturnValue(null); // sources don't exist yet (triggers addSource)
    });

    // ── Test 1: Panel has role="region" and aria-label ────────────────────────
    it('layer panel has role="region" and aria-label="Map layer toggles"', async () => {
        const { findByRole } = render(
            <MapView projectId="proj-1" useMartinTiles={true} />,
        );
        triggerMapLoad();

        const region = await findByRole('region', { name: 'Map layer toggles' });
        expect(region).toBeTruthy();
    });

    // ── Test 2: All 7 layers render as toggles with correct labels ────────────
    it('renders a toggle for each of the 7 silver layers with correct labels', async () => {
        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();

        const checkboxes = await screen.findAllByRole('checkbox');
        expect(checkboxes).toHaveLength(MVT_LAYERS.length);
        expect(MVT_LAYERS.length).toBe(7);
    });

    // ── Test 3: Each label text matches MvtLayerDef.label ────────────────────
    it('each toggle label matches the layer.label field from MVT_LAYERS', async () => {
        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();

        for (const layer of MVT_LAYERS) {
            const label = await screen.findByText(layer.label);
            expect(label).toBeTruthy();
        }
    });

    // ── Test 4: Default visibility matches MVT_DEFAULT_VISIBILITY ─────────────
    it('checkboxes default to checked state matching MVT_DEFAULT_VISIBILITY', async () => {
        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();

        const checkboxes = await screen.findAllByRole('checkbox') as HTMLInputElement[];
        // Map checkboxes by position to layer IDs (same order as MVT_LAYERS)
        MVT_LAYERS.forEach((layer, idx) => {
            const expectedChecked = MVT_DEFAULT_VISIBILITY[layer.id] ?? true;
            expect(checkboxes[idx].checked).toBe(expectedChecked);
        });
    });

    // ── Test 5: Toggling a layer calls setLayoutProperty on the correct id ────
    it('toggling a layer calls setLayoutProperty with the correct mvt-{id} layer id', async () => {
        // Reset getSource to return an object so layers get added
        mockGetSource.mockReturnValue({ setTiles: vi.fn() });
        mockGetLayer.mockReturnValue(false); // layers don't exist yet → triggers addLayer

        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();

        // After map is ready, layers get added. Now simulate getLayer returning true.
        mockGetLayer.mockReturnValue(true);

        // Find the collars checkbox (last item) and toggle it
        const collarsLayer = MVT_LAYERS.find((l) => l.id === 'collars')!;
        const collarsLabel = await screen.findByText(collarsLayer.label);
        const checkboxWrapper = collarsLabel.closest('div');
        const checkbox = checkboxWrapper?.querySelector('input[type="checkbox"]') as HTMLInputElement;
        expect(checkbox).toBeTruthy();

        // Uncheck it — should call setLayoutProperty with 'none'
        fireEvent.click(checkbox);

        // setLayoutProperty should have been called for mvt-collars with 'none'
        const calls = mockSetLayoutProperty.mock.calls;
        const collarCall = calls.find(([id, prop, val]) =>
            id === 'mvt-collars' && prop === 'visibility' && val === 'none',
        );
        expect(collarCall).toBeDefined();
    });

    // ── Test 6: Outline layers toggle both fill and outline ───────────────────
    it('toggling boundaries applies setLayoutProperty to BOTH mvt-boundaries AND mvt-boundaries-outline', async () => {
        mockGetSource.mockReturnValue({ setTiles: vi.fn() });
        mockGetLayer.mockReturnValue(false);

        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();
        mockGetLayer.mockReturnValue(true);

        const boundariesLayer = MVT_LAYERS.find((l) => l.id === 'boundaries')!;
        const label = await screen.findByText(boundariesLayer.label);
        const checkboxWrapper = label.closest('div');
        const checkbox = checkboxWrapper?.querySelector('input[type="checkbox"]') as HTMLInputElement;

        fireEvent.click(checkbox); // toggle to hidden

        const calls = mockSetLayoutProperty.mock.calls;
        const fillCall = calls.find(([id, prop]) => id === 'mvt-boundaries' && prop === 'visibility');
        const outlineCall = calls.find(([id, prop]) => id === 'mvt-boundaries-outline' && prop === 'visibility');

        expect(fillCall).toBeDefined();
        expect(outlineCall).toBeDefined();
    });

    // ── Test 7: Formations outline layer also toggled ─────────────────────────
    it('toggling formations applies setLayoutProperty to BOTH mvt-formations AND mvt-formations-outline', async () => {
        mockGetSource.mockReturnValue({ setTiles: vi.fn() });
        mockGetLayer.mockReturnValue(false);

        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();
        mockGetLayer.mockReturnValue(true);

        const formationsLayer = MVT_LAYERS.find((l) => l.id === 'formations')!;
        const label = await screen.findByText(formationsLayer.label);
        const checkboxWrapper = label.closest('div');
        const checkbox = checkboxWrapper?.querySelector('input[type="checkbox"]') as HTMLInputElement;

        fireEvent.click(checkbox);

        const calls = mockSetLayoutProperty.mock.calls;
        const fillCall = calls.find(([id, prop]) => id === 'mvt-formations' && prop === 'visibility');
        const outlineCall = calls.find(([id, prop]) => id === 'mvt-formations-outline' && prop === 'visibility');

        expect(fillCall).toBeDefined();
        expect(outlineCall).toBeDefined();
    });

    // ── Test 8: Seismic outline layer also toggled ────────────────────────────
    it('toggling seismic applies setLayoutProperty to BOTH mvt-seismic AND mvt-seismic-outline', async () => {
        mockGetSource.mockReturnValue({ setTiles: vi.fn() });
        mockGetLayer.mockReturnValue(false);

        render(<MapView projectId="proj-1" useMartinTiles={true} />);
        triggerMapLoad();
        mockGetLayer.mockReturnValue(true);

        const seismicLayer = MVT_LAYERS.find((l) => l.id === 'seismic')!;
        const label = await screen.findByText(seismicLayer.label);
        const checkboxWrapper = label.closest('div');
        const checkbox = checkboxWrapper?.querySelector('input[type="checkbox"]') as HTMLInputElement;

        fireEvent.click(checkbox);

        const calls = mockSetLayoutProperty.mock.calls;
        const fillCall = calls.find(([id, prop]) => id === 'mvt-seismic' && prop === 'visibility');
        const outlineCall = calls.find(([id, prop]) => id === 'mvt-seismic-outline' && prop === 'visibility');

        expect(fillCall).toBeDefined();
        expect(outlineCall).toBeDefined();
    });

    // ── Test 9: Panel is hidden in compact mode ────────────────────────────────
    it('layer toggle panel is NOT rendered when compact=true', () => {
        render(<MapView projectId="proj-1" useMartinTiles={true} compact={true} />);
        triggerMapLoad();

        const region = screen.queryByRole('region', { name: 'Map layer toggles' });
        expect(region).toBeNull();
    });

    // ── Test 10: Layer.label values match expected human-readable strings ──────
    it('layer labels match expected human-readable values from MvtLayerDef', () => {
        const labelMap: Record<string, string> = {
            boundaries:          'Boundaries',
            formations:          'Formations',
            seismic:             'Seismic',
            traces:              'Drill traces',
            'historic-workings': 'Historic workings',
            geochem:             'Geochem samples',
            collars:             'Collars',
        };

        for (const layer of MVT_LAYERS) {
            expect(layer.label).toBe(labelMap[layer.id]);
        }
    });
});
