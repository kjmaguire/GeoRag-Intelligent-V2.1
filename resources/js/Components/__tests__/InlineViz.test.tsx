// @ts-nocheck
/**
 * InlineViz.test.tsx — §6b P3
 *
 * Pins the chart-card dispatcher contract from the React side. The
 * backend dispatcher (`_build_chat_card_payloads`) is covered by
 * src/fastapi/tests/test_chat_card_payloads.py (30 tests); this file
 * covers the TypeScript side: given a vizPayload with chart_type=X,
 * the right card mounts. Plus the orthogonal map/viz/dismiss state.
 *
 * Strategy: mock each child card component to render a sentinel
 * marker so the test asserts on which CARD mounted without depending
 * on the card's internal layout. The Suspense fallback also gets
 * sentinel content so loading states are observable.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import type { VizPayload } from '@/types';
import { KNOWN_VIZ_CHART_TYPES } from '@/types';

// Mock each lazy-loaded child component BEFORE importing InlineViz so the
// React.lazy() Suspense boundaries resolve to the sentinels synchronously.
// Each mock returns a unique data-testid that asserts pin against.
vi.mock('../MapView', () => ({
    default: ({ inlineGeoJson }: { inlineGeoJson: unknown }) => (
        <div data-testid="mock-map-view">
            map:{(inlineGeoJson as { features?: unknown[] } | undefined)?.features?.length ?? 0}
        </div>
    ),
}));
vi.mock('../StripLogViewer', () => ({
    default: ({ holeId }: { holeId: string }) => (
        <div data-testid="mock-strip-log">strip:{holeId}</div>
    ),
}));
vi.mock('../GeoPlot', () => ({
    default: () => <div data-testid="mock-geo-plot">plot</div>,
}));
vi.mock('../KnowledgeGraph', () => ({
    default: ({ graphNodes }: { graphNodes: unknown[] }) => (
        <div data-testid="mock-knowledge-graph">graph:{graphNodes?.length ?? 0}</div>
    ),
}));
vi.mock('../DrillTrace3D', () => ({
    default: ({ collars }: { collars: unknown[] }) => (
        <div data-testid="mock-drill-trace-3d">trace:{collars?.length ?? 0}</div>
    ),
}));
vi.mock('../TimelineCard', () => ({
    default: ({ swimlanes }: { swimlanes: unknown[] }) => (
        <div data-testid="mock-timeline">timeline:{swimlanes?.length ?? 0}</div>
    ),
}));
vi.mock('../CoverageTableCard', () => ({
    default: ({ rows }: { rows: unknown[] }) => (
        <div data-testid="mock-coverage-table">coverage:{rows?.length ?? 0}</div>
    ),
}));
vi.mock('../StereonetCard', () => ({
    default: ({ meta }: { meta: { image_base64?: string } }) => (
        <div data-testid="mock-stereonet">stereonet:{(meta?.image_base64 ?? '').slice(0, 8)}</div>
    ),
}));

import InlineViz from '../InlineViz';


// ---------------------------------------------------------------------------
// Fixture builders — one per chart_type, with minimal-valid meta
// ---------------------------------------------------------------------------


const MAP_PAYLOAD = {
    geojson: {
        type: 'FeatureCollection' as const,
        features: [
            {
                type: 'Feature' as const,
                geometry: { type: 'Point', coordinates: [-105.5, 44.5] },
                properties: { hole_id: 'H-001' },
            },
        ],
    },
    bbox: [-106, 44, -105, 45] as [number, number, number, number],
    label: 'Test map',
};

const VIZ_STRIP: VizPayload = {
    chart_type: 'downhole_strip',
    plotly_layout: { meta: { hole_id: 'ECK-22-001', collar_id: 'c-1' } },
};

const VIZ_HISTOGRAM: VizPayload = {
    chart_type: 'assay_histogram',
    plotly_data: [{ x: [1, 2, 3], type: 'histogram' }],
    plotly_layout: { meta: {} },
};

const VIZ_GRAPH: VizPayload = {
    chart_type: 'graph_viz',
    plotly_layout: { meta: { nodes: [{ id: 'a' }, { id: 'b' }], edges: [] } },
};

const VIZ_3D: VizPayload = {
    chart_type: 'drill_trace_3d',
    plotly_layout: {
        meta: {
            collars: [{ collar_id: 'c-1' }, { collar_id: 'c-2' }],
            intervals: [],
            structures: [],
        },
    },
};

const VIZ_TIMELINE: VizPayload = {
    chart_type: 'technique_timeline',
    plotly_layout: {
        meta: {
            swimlanes: [{ technique: 'DDH', year_start: 2022, year_end: 2022 }],
            breakdown_table: [],
        },
    },
};

const VIZ_COVERAGE: VizPayload = {
    chart_type: 'coverage_table',
    plotly_layout: {
        meta: {
            rows: [{ attribute: 'assays', collars_with_data: 8, collars_total: 10 }],
            ingest_gap: { indexed: 100, processed: 100, gap_pct: 0 },
        },
    },
};

const VIZ_STEREONET: VizPayload = {
    chart_type: 'stereonet',
    plotly_layout: {
        meta: {
            image_base64: 'iVBORw0KGgo-test-bytes',
            projection: 'Schmidt',
            structure_count: 3,
            points: [],
        },
    },
};


// ---------------------------------------------------------------------------
// Null safety
// ---------------------------------------------------------------------------


describe('InlineViz — null safety', () => {
    it('renders nothing when both map and viz are null', () => {
        const { container } = render(<InlineViz mapPayload={null} vizPayload={null} />);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when both map and viz are undefined', () => {
        const { container } = render(<InlineViz />);
        expect(container.firstChild).toBeNull();
    });
});


// ---------------------------------------------------------------------------
// Map-only / viz-only / both paths
// ---------------------------------------------------------------------------


describe('InlineViz — orthogonal map/viz states', () => {
    it('renders ONLY the map card when vizPayload is null', async () => {
        render(<InlineViz mapPayload={MAP_PAYLOAD} vizPayload={null} />);
        expect(await screen.findByTestId('mock-map-view')).toBeTruthy();
        expect(screen.queryByTestId('mock-strip-log')).toBeNull();
        expect(screen.queryByTestId('mock-stereonet')).toBeNull();
    });

    it('renders ONLY the viz card when mapPayload is null', async () => {
        render(<InlineViz mapPayload={null} vizPayload={VIZ_STEREONET} />);
        expect(await screen.findByTestId('mock-stereonet')).toBeTruthy();
        expect(screen.queryByTestId('mock-map-view')).toBeNull();
    });

    it('renders BOTH cards when both payloads are present', async () => {
        render(<InlineViz mapPayload={MAP_PAYLOAD} vizPayload={VIZ_STEREONET} />);
        expect(await screen.findByTestId('mock-map-view')).toBeTruthy();
        expect(await screen.findByTestId('mock-stereonet')).toBeTruthy();
    });
});


// ---------------------------------------------------------------------------
// Per-card dispatch — one test per chart_type
// ---------------------------------------------------------------------------


describe('InlineViz — chart_type → card dispatch', () => {
    it.each([
        ['downhole_strip',     VIZ_STRIP,      'mock-strip-log'],
        ['assay_histogram',    VIZ_HISTOGRAM,  'mock-geo-plot'],
        ['cross_section',      { ...VIZ_HISTOGRAM, chart_type: 'cross_section' as const }, 'mock-geo-plot'],
        ['graph_viz',          VIZ_GRAPH,      'mock-knowledge-graph'],
        ['drill_trace_3d',     VIZ_3D,         'mock-drill-trace-3d'],
        ['technique_timeline', VIZ_TIMELINE,   'mock-timeline'],
        ['coverage_table',     VIZ_COVERAGE,   'mock-coverage-table'],
        ['stereonet',          VIZ_STEREONET,  'mock-stereonet'],
    ])('chart_type=%s mounts %s', async (_label, payload, expectedTestId) => {
        render(<InlineViz vizPayload={payload as VizPayload} />);
        expect(await screen.findByTestId(expectedTestId)).toBeTruthy();
    });

    it('KNOWN_VIZ_CHART_TYPES export matches the dispatch cases above', () => {
        // Belt-and-braces: when a new chart_type lands in the dispatcher,
        // it has to be added to KNOWN_VIZ_CHART_TYPES too (for the sentry
        // tag drift check) — this test pins both sides in sync.
        expect(new Set(KNOWN_VIZ_CHART_TYPES)).toEqual(new Set([
            'downhole_strip', 'assay_histogram', 'cross_section',
            'graph_viz', 'drill_trace_3d', 'technique_timeline',
            'coverage_table', 'stereonet',
        ]));
    });
});


// ---------------------------------------------------------------------------
// Empty-meta fallthrough
// ---------------------------------------------------------------------------


describe('InlineViz — empty meta falls through cleanly', () => {
    it('chart_type=downhole_strip with no hole_id renders nothing', () => {
        const payload: VizPayload = {
            chart_type: 'downhole_strip',
            plotly_layout: { meta: {} },
        };
        const { container } = render(<InlineViz vizPayload={payload} />);
        expect(container.firstChild).toBeNull();
    });

    it('chart_type=stereonet with empty image_base64 renders nothing', () => {
        const payload: VizPayload = {
            chart_type: 'stereonet',
            plotly_layout: { meta: { image_base64: '' } },
        };
        const { container } = render(<InlineViz vizPayload={payload} />);
        expect(container.firstChild).toBeNull();
    });

    it('chart_type=drill_trace_3d with empty collars renders nothing', () => {
        const payload: VizPayload = {
            chart_type: 'drill_trace_3d',
            plotly_layout: { meta: { collars: [] } },
        };
        const { container } = render(<InlineViz vizPayload={payload} />);
        expect(container.firstChild).toBeNull();
    });

    it('chart_type=assay_histogram with empty plotly_data renders nothing', () => {
        const payload: VizPayload = {
            chart_type: 'assay_histogram',
            plotly_data: [],
            plotly_layout: { meta: {} },
        };
        const { container } = render(<InlineViz vizPayload={payload} />);
        expect(container.firstChild).toBeNull();
    });

    it('unknown chart_type renders nothing (drift safety)', () => {
        // A new chart_type added to the backend dispatcher without a
        // matching frontend branch — should silently degrade rather
        // than crash. This is the canary path for the §6b P6 sentry
        // card.type='unknown' tag.
        const payload: VizPayload = {
            chart_type: 'future_card_type' as unknown as VizPayload['chart_type'],
            plotly_layout: { meta: { rows: [{}] } },
        };
        const { container } = render(<InlineViz vizPayload={payload} />);
        expect(container.firstChild).toBeNull();
    });
});


// ---------------------------------------------------------------------------
// Per-card dismiss behaviour
// ---------------------------------------------------------------------------


describe('InlineViz — dismiss behaviour', () => {
    it('clicking the map close button hides the map card', async () => {
        render(<InlineViz mapPayload={MAP_PAYLOAD} vizPayload={null} />);
        expect(await screen.findByTestId('mock-map-view')).toBeTruthy();

        const closeButton = screen.getByLabelText('Hide visualization');
        fireEvent.click(closeButton);

        // Map card gone; nothing else to render
        expect(screen.queryByTestId('mock-map-view')).toBeNull();
    });

    it('clicking the viz close button hides the viz card', async () => {
        render(<InlineViz mapPayload={null} vizPayload={VIZ_STEREONET} />);
        expect(await screen.findByTestId('mock-stereonet')).toBeTruthy();

        const closeButton = screen.getByLabelText('Hide visualization');
        fireEvent.click(closeButton);

        expect(screen.queryByTestId('mock-stereonet')).toBeNull();
    });

    it('dismissing the map keeps the viz card visible', async () => {
        render(<InlineViz mapPayload={MAP_PAYLOAD} vizPayload={VIZ_STEREONET} />);
        await screen.findByTestId('mock-map-view');
        await screen.findByTestId('mock-stereonet');

        // Both cards present → two "Hide visualization" buttons. Click the
        // first (the map's).
        const closeButtons = screen.getAllByLabelText('Hide visualization');
        expect(closeButtons.length).toBe(2);
        fireEvent.click(closeButtons[0]);

        expect(screen.queryByTestId('mock-map-view')).toBeNull();
        expect(screen.queryByTestId('mock-stereonet')).toBeTruthy();
    });
});


// ---------------------------------------------------------------------------
// Data is propagated to child mocks
// ---------------------------------------------------------------------------


describe('InlineViz — data prop propagation', () => {
    it('passes meta.hole_id through to StripLogViewer', async () => {
        render(<InlineViz vizPayload={VIZ_STRIP} />);
        const node = await screen.findByTestId('mock-strip-log');
        expect(node.textContent).toContain('strip:ECK-22-001');
    });

    it('passes meta.collars count through to DrillTrace3D', async () => {
        render(<InlineViz vizPayload={VIZ_3D} />);
        const node = await screen.findByTestId('mock-drill-trace-3d');
        expect(node.textContent).toContain('trace:2');
    });

    it('passes meta.swimlanes count through to TimelineCard', async () => {
        render(<InlineViz vizPayload={VIZ_TIMELINE} />);
        const node = await screen.findByTestId('mock-timeline');
        expect(node.textContent).toContain('timeline:1');
    });

    it('passes meta.image_base64 prefix through to StereonetCard', async () => {
        render(<InlineViz vizPayload={VIZ_STEREONET} />);
        const node = await screen.findByTestId('mock-stereonet');
        expect(node.textContent).toContain('stereonet:iVBORw0K');
    });

    it('passes mapPayload feature count through to MapView', async () => {
        render(<InlineViz mapPayload={MAP_PAYLOAD} />);
        const node = await screen.findByTestId('mock-map-view');
        expect(node.textContent).toContain('map:1');
    });
});
