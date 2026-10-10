/**
 * The Workspace 3D views draw true-scale scenes in ONE local frame
 * (GIS audit 2026-10, findings 3 and 4).
 *
 * Finding 3: they added metre offsets to the raw `silver.collars.easting /
 * northing`, which are in the CRS and units of whichever upload the collar
 * came from, and labelled the axis "Easting (m)". The position is the
 * collar's lng/lat, in a local east/north frame in metres.
 *
 * Finding 4: five of them set `aspectmode: 'manual'` with a 1:1:0.6 box, which
 * draws every dip steeper or flatter than it is.
 */
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EAST_AXIS_TITLE, NORTH_AXIS_TITLE } from '@/lib/desurvey';
import { metresPerDegree } from '@/lib/localEnu';

type PlotProps = { data?: Array<Record<string, unknown>>; layout?: Record<string, unknown> };
const plotted: PlotProps = {};

vi.mock('@/Components/GeoPlot', () => ({
    default: (props: PlotProps) => {
        plotted.data = props.data;
        plotted.layout = props.layout;
        return null;
    },
}));
vi.mock('../../GeoPlot', () => ({
    default: (props: PlotProps) => {
        plotted.data = props.data;
        plotted.layout = props.layout;
        return null;
    },
}));

import MultiHole3DTrace from '@/Components/Analytics/MultiHole3DTrace';
import AssayComposites3DView from '../AssayComposites3DView';
import { Borehole3DView } from '../Borehole3DView';
import CommoditySamples3DView from '../CommoditySamples3DView';
import SignificantIntersections3DView from '../SignificantIntersections3DView';
import StructureDiscs3DView from '../StructureDiscs3DView';

afterEach(() => {
    cleanup();
    plotted.data = undefined;
    plotted.layout = undefined;
});

// The same neighbourhood stored two ways: UTM metres and US-ft state plane.
// lng/lat is consistent (A and B are 0.01 degrees of longitude apart, ~591 m
// at 58 N); the stored easting/northing are millions apart and mean nothing
// together.
const SEPARATION_M = 0.01 * metresPerDegree(58).lon;
const COLLARS = [
    {
        collar_id: 'a',
        hole_id: 'A',
        hole_id_canonical: 'A',
        lng: -105.0,
        lat: 58.0,
        easting: 500000,
        northing: 6427000,
        total_depth: 100,
        azimuth: 0,
        dip: -90,
        elevation: 300,
    },
    {
        collar_id: 'b',
        hole_id: 'B',
        hole_id_canonical: 'B',
        lng: -104.99,
        lat: 58.0,
        easting: 2100000,
        northing: 600000,
        total_depth: 100,
        azimuth: 0,
        dip: -90,
        elevation: 300,
    },
];

function scene(): Record<string, unknown> {
    return (plotted.layout as { scene: Record<string, unknown> }).scene;
}

/** First x of the first line trace with this name: where the hole starts. */
function collarX(name: string): number {
    const t = (plotted.data ?? []).find((d) => d.name === name && d.mode === 'lines');
    expect(t, `no line trace named ${name}`).toBeDefined();
    const xs = (t as { x: Array<number | null> }).x.filter((v): v is number => v !== null);
    return xs[0];
}

const VIEWS: Array<{ name: string; draw: () => void; ghostNames: [string, string] }> = [
    {
        name: 'Borehole3DView',
        ghostNames: ['A', 'B'],
        draw: () => {
            render(
                <Borehole3DView
                    holes={COLLARS.map((c) => ({
                        collar_id: c.collar_id,
                        hole_id: c.hole_id,
                        total_depth: 100,
                        easting: c.easting,
                        northing: c.northing,
                        lat: c.lat,
                        lng: c.lng,
                        bands: [{ from: 0, to: 100, code: 'SST', color: '#cccccc' }],
                    }))}
                    collars={COLLARS}
                />,
            );
        },
    },
    {
        name: 'SignificantIntersections3DView',
        ghostNames: ['A', 'B'],
        draw: () => {
            render(
                <SignificantIntersections3DView
                    collars={COLLARS}
                    intersections={[
                        {
                            collar_id: 'a',
                            element: 'Au',
                            cutoff_grade: 1,
                            from_depth: 10,
                            to_depth: 20,
                            true_width_m: null,
                            weighted_avg: 2,
                            unit: 'g/t',
                            peak_value: null,
                            peak_depth: null,
                            zone_name: null,
                        },
                    ]}
                />,
            );
        },
    },
    {
        name: 'AssayComposites3DView',
        ghostNames: ['A', 'B'],
        draw: () => {
            render(
                <AssayComposites3DView
                    collars={COLLARS}
                    composites={[
                        {
                            collar_id: 'a',
                            element: 'Au',
                            from_depth: 10,
                            to_depth: 20,
                            weighted_avg: 2,
                            unit: 'g/t',
                            cutoff_grade: null,
                            sample_count: 3,
                        },
                    ]}
                    elements={[{ element: 'Au', count: 1 }]}
                />,
            );
        },
    },
    {
        name: 'CommoditySamples3DView',
        ghostNames: ['A', 'B'],
        draw: () => {
            render(
                <CommoditySamples3DView
                    collars={COLLARS}
                    samples={[
                        {
                            collar_id: 'a',
                            from_depth: 10,
                            to_depth: 20,
                            sample_type: 'core',
                            grades: { U3O8_pct: 0.2 },
                        },
                    ]}
                    commodityKeys={[{ key: 'U3O8_pct', count: 1 }]}
                />,
            );
        },
    },
    {
        name: 'StructureDiscs3DView',
        ghostNames: ['A', 'B'],
        draw: () => {
            render(
                <StructureDiscs3DView
                    collars={COLLARS}
                    structures={[
                        {
                            collar_id: 'a',
                            strike_deg: 45,
                            dip_deg: 30,
                            measurement_kind: 'bedding',
                            depth_m: 20,
                            pole_trend_deg: 225,
                            pole_plunge_deg: 60,
                            display_color: null,
                            display_symbol: null,
                            confidence: null,
                        },
                    ]}
                />,
            );
        },
    },
];

describe.each(VIEWS)('$name', ({ draw, ghostNames }) => {
    it('draws at true scale: aspectmode data, no manual aspect box', () => {
        draw();
        expect(scene().aspectmode).toBe('data');
        expect(scene().aspectratio).toBeUndefined();
    });

    it('labels the axes as local metres, not as a map easting/northing', () => {
        draw();
        const title = (axis: string) => (scene()[axis] as { title: { text: string } }).title.text;
        expect(title('xaxis')).toBe(EAST_AXIS_TITLE);
        expect(title('yaxis')).toBe(NORTH_AXIS_TITLE);
    });

    it('places the two holes by lng/lat, 591 m apart, not by their stored easting', () => {
        draw();
        const gap = collarX(ghostNames[1]) - collarX(ghostNames[0]);
        expect(gap).toBeGreaterThan(SEPARATION_M - 1);
        expect(gap).toBeLessThan(SEPARATION_M + 1);
    });
});

describe('MultiHole3DTrace', () => {
    function drawTrace(collars = COLLARS) {
        render(
            <MultiHole3DTrace
                collars={collars.map((c) => ({
                    collar_id: c.collar_id,
                    hole_id: c.hole_id,
                    azimuth: c.azimuth,
                    dip: c.dip,
                    elevation: c.elevation,
                    lng: c.lng,
                    lat: c.lat,
                    easting: c.easting,
                    northing: c.northing,
                    total_depth: c.total_depth,
                    hole_type: 'Diamond',
                    status: 'Completed',
                }))}
                surveys={[
                    { collar_id: 'a', depth: 0, azimuth: 0, dip: -90 },
                    { collar_id: 'a', depth: 100, azimuth: 0, dip: -90 },
                    { collar_id: 'b', depth: 0, azimuth: 0, dip: -90 },
                    { collar_id: 'b', depth: 100, azimuth: 0, dip: -90 },
                ]}
            />,
        );
    }

    it('keeps its true-scale aspect and relabels the axes', () => {
        drawTrace();
        expect(scene().aspectmode).toBe('data');
        const title = (axis: string) => (scene()[axis] as { title: { text: string } }).title.text;
        expect(title('xaxis')).toBe(EAST_AXIS_TITLE);
        expect(title('yaxis')).toBe(NORTH_AXIS_TITLE);
    });

    it('places holes by lng/lat', () => {
        drawTrace();
        const collarMarkers = (plotted.data ?? []).filter(
            (d) => d.mode === 'markers' && typeof d.hovertext === 'string' && String(d.hovertext).endsWith(' collar'),
        ) as Array<{ x: number[]; hovertext: string }>;
        const x = (id: string) => collarMarkers.find((m) => m.hovertext.startsWith(`${id} collar`))!.x[0];
        expect(x('B') - x('A')).toBeGreaterThan(SEPARATION_M - 1);
        expect(x('B') - x('A')).toBeLessThan(SEPARATION_M + 1);
    });

    it('says so when no collar has a geographic position', () => {
        const noPosition = COLLARS.map((c) => ({ ...c, lng: null, lat: null }));
        const { container } = render(
            <MultiHole3DTrace
                collars={noPosition.map((c) => ({
                    collar_id: c.collar_id,
                    hole_id: c.hole_id,
                    azimuth: 0,
                    dip: -90,
                    elevation: 300,
                    lng: null,
                    lat: null,
                    easting: c.easting,
                    northing: c.northing,
                    hole_type: null,
                    status: null,
                }))}
                surveys={[]}
            />,
        );
        expect(container.textContent).toContain('2 have no geographic position');
    });
});
