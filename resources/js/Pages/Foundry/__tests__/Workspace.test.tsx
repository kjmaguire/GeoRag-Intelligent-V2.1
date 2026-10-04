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
import { act, fireEvent, render, screen } from '@testing-library/react';
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
    Link: ({ href, children, ...rest }: { href: string; children: ReactNode }) => <a href={href} {...rest}>{children}</a>,
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
            project_id: 'p-1', project_name: 'Red Star', slug: 'red-star', company: null,
            commodity: 'Gold', region: null, crs_epsg: 26912, data_version: 4,
        },
        project_extent: null,
        project_summary: { total_drilled_m: 0, mean_td_m: null, ore_hole_count: 0, total_ore_thickness_m: 0, mean_u3o8_pct: null },
        project_aoi: null,
        collars: [{
            collar_id: 'c-1', hole_id: 'RS-1', hole_id_canonical: 'RS-1', easting: 500000, northing: 6000000,
            total_depth: 200, lat: 54, lng: -110, ore_bands: 0, ore_thickness_m: 0, azimuth: 45, dip: -60, elevation: 400,
        }],
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
        truncation: { collars: { shown: 1, total: 1, truncated: false }, interval_holes: { shown: 1, total: 1, truncated: false } },
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
        renderPage(props({
            collars: [],
            empty: true,
            project_extent: [-106, 57, -105, 58],
            project_layers: [{ id: 'imported-polygons', label: 'Imported areas', count: 3, on: true }],
        }));
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
        renderPage(props({
            first_holes_intervals: [], surveys_3d: [], structures_3d: [], assay_composites_3d: [],
            assay_elements_3d: [], significant_intersections_3d: [], structures_visual_3d: [],
            commodity_samples_3d: [], commodity_keys_3d: [], survey_holes_downsampled: 0,
        }));
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
        expect(screen.getByRole('link', { name: /open hole page/i })).toHaveAttribute('href', '/projects/red-star/holes/c-1/detail');
        // The regional reference column is opt-in, not presented as this project's (FE-25).
        expect(screen.queryByText(/Athabasca Group · Wollaston Domain/)).toBeNull();
        expect(screen.getByRole('button', { name: /show a regional reference column/i })).toBeInTheDocument();
    });

    it('derives quick prompts from the commodity (FE-25)', () => {
        renderPage(props());
        expect(screen.getByText('Which holes have the best Gold intervals?')).toBeInTheDocument();
        expect(screen.queryByText(/Smith Ranch|U₃O₈/)).toBeNull();
    });

    it('uses plain language in every empty 3D view (no table names, tiers, pipelines)', () => {
        window.history.replaceState({}, '', '/projects/red-star/workspace?mode=3d');
        const { container } = renderPage(props({
            first_holes_intervals: [], surveys_3d: [], structures_3d: [], assay_composites_3d: [],
            assay_elements_3d: [], significant_intersections_3d: [], structures_visual_3d: [],
            commodity_samples_3d: [], commodity_keys_3d: [], survey_holes_downsampled: 0,
        }));
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
});
