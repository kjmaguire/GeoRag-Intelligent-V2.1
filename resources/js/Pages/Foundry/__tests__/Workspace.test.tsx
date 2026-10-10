/**
 * Foundry/Workspace — page-level behaviours from the 2026-09-29 audit.
 *
 *   FE-3  a project with map layers but no collars still gets the map
 *   FE-11 3D props are deferred: skeleton until they arrive; ingest events
 *         reload only what they affect
 *   FE-15 LOGS links to the per-hole page
 *   FE-18 the hole's coordinates are labelled with the project CRS
 *   FE-25 quick prompts follow the project commodity
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

const inertia = vi.hoisted(() => ({
    pageProps: {} as Record<string, unknown>,
    reload: vi.fn(),
    get: vi.fn(),
    dataUpdated: null as null | ((e: { affected_types: string[] }) => void),
}));

vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    Link: ({ href, children, ...rest }: { href: string; children: ReactNode }) => (
        <a href={href} {...rest}>
            {children}
        </a>
    ),
    router: { reload: inertia.reload, get: inertia.get, visit: vi.fn() },
    usePage: () => ({ props: inertia.pageProps }),
    Deferred: ({ data, fallback, children }: { data: string | string[]; fallback: ReactNode; children: ReactNode }) => {
        const keys = Array.isArray(data) ? data : [data];
        return <>{keys.every((k) => inertia.pageProps[k] !== undefined) ? children : fallback}</>;
    },
}));
vi.mock('@/Layouts/AppLayout', () => ({ default: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock('@/Components/Foundry/WorkspaceMap', () => ({
    WorkspaceMap: (p: { projectExtent: unknown; dataVersion: number }) => (
        <div data-testid="workspace-map" data-extent={JSON.stringify(p.projectExtent)} data-version={p.dataVersion} />
    ),
}));
vi.mock('@/Hooks/useWorkspaceDataUpdated', () => ({
    useWorkspaceDataUpdated: (_id: string, cb: (e: { affected_types: string[] }) => void) => {
        inertia.dataUpdated = cb;
    },
}));

import FoundryWorkspace from '../Workspace';

function props(over: Record<string, unknown> = {}): Record<string, unknown> {
    return {
        project: {
            project_id: 'p-1',
            project_name: 'Red Star',
            slug: 'red-star',
            company: null,
            commodity: 'Gold',
            region: null,
            crs_epsg: 26912,
            data_version: 4,
        },
        project_extent: null,
        project_summary: {
            total_drilled_m: 0,
            mean_td_m: null,
            ore_hole_count: 0,
            total_ore_thickness_m: 0,
            mean_u3o8_pct: null,
        },
        project_aoi: null,
        collars: [
            {
                collar_id: 'c-1',
                hole_id: 'RS-1',
                hole_id_canonical: 'RS-1',
                easting: 500000,
                northing: 6000000,
                total_depth: 200,
                lat: 54,
                lng: -110,
                ore_bands: 0,
                ore_thickness_m: 0,
                azimuth: 45,
                dip: -60,
                elevation: 400,
            },
        ],
        sections_count: 0,
        intervals_count: 0,
        structures_count: 0,
        structures_visual_count: 0,
        well_log_curves_count: 0,
        curve_summary: [],
        log_tracks: [],
        log_available_curves: [],
        log_selected_curves: [],
        log_curves_max: 12,
        log_hole_id: 'RS-1',
        log_depth_max: 200,
        log_hole_options: ['RS-1'],
        log_hole_total_depth: 200,
        log_hole_easting: 500000,
        log_hole_northing: 6000000,
        log_lithology_intervals: [{ from: 0, to: 10, code: 'GRN', label: 'Granite', color: '#999' }],
        log_alteration_intervals: [],
        log_mineralization_intervals: [],
        project_layers: [{ id: 'collars', label: 'Collars', count: 1, on: true }],
        strat_units: [],
        strat_source: 'reference',
        project_country: 'CA',
        empty: false,
        truncation: {
            collars: { shown: 1, total: 1, truncated: false },
            interval_holes: { shown: 1, total: 1, truncated: false },
        },
        ...over,
    };
}

function renderPage(p: Record<string, unknown>) {
    inertia.pageProps = p;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return render(<FoundryWorkspace {...(p as any)} />);
}

beforeEach(() => {
    window.history.replaceState({}, '', '/projects/red-star/workspace');
});
afterEach(() => {
    vi.clearAllMocks();
});

describe('Foundry/Workspace', () => {
    it('shows the map for a GIS-only project instead of "no drill data" (FE-3)', () => {
        renderPage(
            props({
                collars: [],
                empty: true,
                project_extent: [-106, 57, -105, 58],
                project_layers: [{ id: 'imported-polygons', label: 'Imported areas', count: 3, on: true }],
            }),
        );
        const map = screen.getByTestId('workspace-map');
        expect(map).toHaveAttribute('data-extent', '[-106,57,-105,58]');
        expect(map).toHaveAttribute('data-version', '4');
        expect(screen.queryByText(/nothing to show/i)).toBeNull();
    });

    it('keeps the empty state when there is truly nothing', () => {
        renderPage(props({ collars: [], empty: true, project_layers: [] }));
        expect(screen.getByText(/nothing to show in this project yet/i)).toBeInTheDocument();
    });

    it('shows a pulsing skeleton for 3D until the deferred group arrives (FE-11)', () => {
        window.history.replaceState({}, '', '/projects/red-star/workspace?mode=3d');
        renderPage(props());
        const skeleton = screen.getByTestId('deferred-skeleton');
        expect(skeleton.className).toContain('animate-pulse');
    });

    it('renders the 3D panel once the deferred group is present', () => {
        window.history.replaceState({}, '', '/projects/red-star/workspace?mode=3d');
        renderPage(
            props({
                first_holes_intervals: [],
                surveys_3d: [],
                structures_3d: [],
                assay_composites_3d: [],
                assay_elements_3d: [],
                significant_intersections_3d: [],
                structures_visual_3d: [],
                commodity_samples_3d: [],
                commodity_keys_3d: [],
                survey_holes_downsampled: 0,
            }),
        );
        expect(screen.queryByTestId('deferred-skeleton')).toBeNull();
        expect(screen.getByText('3D drill trajectories')).toBeInTheDocument();
    });

    it('reloads only what an ingest event affects (FE-11)', () => {
        renderPage(props());
        act(() => inertia.dataUpdated!({ affected_types: ['reports', 'quality', 'review_queue'] }));
        expect(inertia.reload).not.toHaveBeenCalled();

        act(() => inertia.dataUpdated!({ affected_types: ['reports', 'structures'] }));
        expect(inertia.reload).toHaveBeenCalledTimes(1);
        const only = inertia.reload.mock.calls[0][0].only as string[];
        expect(only).toContain('project_layers');
        // MAP mode is on screen: the 3D group is NOT refetched yet.
        expect(only).not.toContain('surveys_3d');

        act(() => inertia.dataUpdated!({ affected_types: ['collars'] }));
        expect(inertia.reload).toHaveBeenLastCalledWith();
    });

    it('labels LOGS coordinates with the project CRS and links the hole page (FE-15, FE-18)', () => {
        window.history.replaceState({}, '', '/projects/red-star/workspace?mode=logs');
        renderPage(props());
        expect(screen.getByText(/EPSG:26912 · E 500,000/)).toBeInTheDocument();
        expect(screen.queryByText(/UTM 13N/)).toBeNull();
        expect(screen.getByRole('link', { name: /open hole page/i })).toHaveAttribute(
            'href',
            '/projects/red-star/holes/c-1/detail',
        );
        // The regional reference column is opt-in, not presented as this project's (FE-25).
        expect(screen.queryByText(/Athabasca Group · Wollaston Domain/)).toBeNull();
        expect(screen.getByRole('button', { name: /show a regional reference column/i })).toBeInTheDocument();
    });

    describe('LOGS depth axis', () => {
        const num = (el: Element, attr: string) => Number(el.getAttribute(attr));
        const curve = {
            curve: 'GAMMA',
            group: 'gamma',
            unit: 'cps',
            label: 'GAMMA (cps)',
            color: '#fff',
            min: 0,
            max: 10,
            points: [
                { depth: 0, value: 1 },
                { depth: 180, value: 5 },
            ],
        };

        it('gives the curve tracks and the geology column ONE axis, deeper than either alone', () => {
            window.history.replaceState({}, '', '/projects/red-star/workspace?mode=logs');
            const { container } = renderPage(
                props({
                    log_tracks: [curve],
                    log_depth_max: 180,
                    // The geology runs deeper than the curves.
                    log_lithology_intervals: [
                        { from: 0, to: 100, code: 'GRN', label: 'Granite', color: '#999' },
                        { from: 100, to: 260, code: 'SST', label: 'Sandstone', color: '#c90' },
                    ],
                }),
            );
            const [curves, strip] = Array.from(container.querySelectorAll('svg')).filter(
                (svg) => svg.getAttribute('role') === 'img' || svg.querySelector('text')?.textContent === 'DEPTH (m)',
            );
            // 260 m of geology -> a 300 m axis for BOTH tracks (the curve track
            // used to end at its own 180 m, the column at its own 260).
            expect(curves.textContent).toContain('300');
            expect(curves.getAttribute('height')).toBe(strip.getAttribute('height'));
            const sst = screen.getByLabelText('SST 100-260 m');
            const plot = num(curves.querySelector('rect')!, 'height');
            expect(num(sst, 'y') + num(sst, 'height')).toBeCloseTo(16 + (260 / 300) * plot, 5);
        });

        it("does not let the API's 600 m placeholder stretch a geology-only hole", () => {
            window.history.replaceState({}, '', '/projects/red-star/workspace?mode=logs');
            renderPage(props({ log_tracks: [], log_depth_max: 600 }));
            // 10 m of granite fits a 25 m axis (the hole's own data, rounded up), not the
            // placeholder's 600 m: 40% of the plot, not 1.7% of it.
            const grn = screen.getByLabelText('GRN 0-10 m');
            const plot = num(grn.closest('svg')!, 'height') - 16 - 4; // the SVG less its top and bottom room
            expect(num(grn, 'height') / plot).toBeCloseTo(10 / 25, 5);
        });
    });

    it('derives quick prompts from the commodity (FE-25)', () => {
        renderPage(props());
        expect(screen.getByText('Which holes have the best Gold intervals?')).toBeInTheDocument();
        expect(screen.queryByText(/Smith Ranch|U₃O₈/)).toBeNull();
    });

    it('uses plain language in every empty 3D view (no table names, tiers, pipelines)', () => {
        window.history.replaceState({}, '', '/projects/red-star/workspace?mode=3d');
        const { container } = renderPage(
            props({
                first_holes_intervals: [],
                surveys_3d: [],
                structures_3d: [],
                assay_composites_3d: [],
                assay_elements_3d: [],
                significant_intersections_3d: [],
                structures_visual_3d: [],
                commodity_samples_3d: [],
                commodity_keys_3d: [],
                survey_holes_downsampled: 0,
            }),
        );
        const internal = /silver|gold\.|bronze|§|ADR-|pipeline|derive_|_visual|\bTier\b/i;
        const expected: Array<[string, RegExp]> = [
            ['Lithology', /No 3D intervals for this project yet/],
            ['Assay Grade', /No assay composites yet/],
            ['Intersections', /No significant intersections yet/],
            ['Commodity Samples', /No commodity samples for this project yet/],
            ['Structure Discs', /No structural measurements to display in 3D yet/],
        ];
        for (const [label, copy] of expected) {
            fireEvent.click(screen.getByRole('button', { name: label }));
            expect(screen.getByText(copy)).toBeInTheDocument();
            expect(container.textContent ?? '').not.toMatch(internal);
        }
    });

    // KEEP THIS TEST LAST. It is the one test here that waits (waitFor, up to 1 s), and
    // when it fails it waits the whole second: long enough for the lazily imported
    // Plotly chunk to finish loading and put its stylesheet into the document. Any
    // test that mounts a 3D view AFTER that then spins in jsdom's getComputedStyle
    // (nested style-rule resolution, effectively forever), so a regression here
    // would hang the run instead of failing it. Last, it can only fail.
    it("labels COMPARE's coordinate rows with the project CRS, not a fixed UTM zone (FE-18)", async () => {
        window.history.replaceState({}, '', '/projects/red-star/workspace?mode=compare');
        const first = (props().collars as Array<Record<string, unknown>>)[0];
        const second = { ...first, collar_id: 'c-2', hole_id: 'RS-2', hole_id_canonical: 'RS-2' };
        vi.spyOn(globalThis, 'fetch').mockImplementation(
            async (input) =>
                new Response(
                    JSON.stringify({
                        hole_id: String(input).includes('/RS-2/') ? 'RS-2' : 'RS-1',
                        collar_id: 'c-1',
                        total_depth: 200,
                        easting: 500000,
                        northing: 6000000,
                        lat: null,
                        lng: null,
                        log_tracks: [],
                        log_depth_max: 600,
                        lithology_intervals: [],
                        alteration_intervals: [],
                        mineralization_intervals: [],
                        ore_bands: 0,
                        ore_thickness_m: 0,
                        mean_u3o8_pct: null,
                    }),
                ),
        );
        renderPage(props({ collars: [first, second] }));

        fireEvent.click(screen.getByRole('button', { name: /use first two/i }));

        // Asserted on the text, not through findByText: on a miss, testing-library
        // would pretty-print this whole page, which is slow enough to look like a hang.
        await waitFor(() => expect(document.body.textContent).toContain('Easting (EPSG:26912)'));
        expect(document.body.textContent).not.toContain('UTM 13N');
    });
});
