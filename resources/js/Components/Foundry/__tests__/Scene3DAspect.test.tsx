/**
 * The Workspace 3D views are drawn to true scale.
 *
 * Their scene is in metres on every axis (offsets from the project centroid,
 * elevation), and they used `aspectmode: 'manual'` with `{ x: 1, y: 1, z: 0.6 }`,
 * which fits whatever the data spans into a fixed 1 : 1 : 0.6 box, so the dip and
 * azimuth read off the plot depended on the shape of the project. The chat card
 * (DrillTrace3D) and two analysis views already draw `aspectmode: 'data'` (GIS-9).
 */
import { cleanup, render } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AssayComposites3DView from '../AssayComposites3DView';
import { Borehole3DView } from '../Borehole3DView';
import CommoditySamples3DView from '../CommoditySamples3DView';
import SignificantIntersections3DView from '../SignificantIntersections3DView';
import StructureDiscs3DView from '../StructureDiscs3DView';
import { SCENE_3D_ASPECT } from '@/lib/desurvey';

interface PlotProps {
    layout?: { scene?: Record<string, unknown> & { zaxis?: { title?: { text?: string } } } };
}
const plotted: { layout?: PlotProps['layout'] } = {};

vi.mock('@/Components/GeoPlot', () => ({
    default: (props: PlotProps) => {
        plotted.layout = props.layout;
        return null;
    },
}));

const collars = [
    {
        collar_id: 'c1',
        hole_id: 'DD-01',
        hole_id_canonical: 'DD-01',
        easting: 500000,
        northing: 6000000,
        // The views place holes by lng/lat in one local frame (GIS audit
        // 2026-10); a collar with no position is not drawn.
        lng: -105.0,
        lat: 54.0,
        total_depth: 600,
        azimuth: 45,
        dip: -60,
        elevation: 412,
    },
    {
        collar_id: 'c2',
        hole_id: 'DD-02',
        hole_id_canonical: 'DD-02',
        easting: 500400,
        northing: 6000100,
        lng: -104.994,
        lat: 54.001,
        total_depth: 300,
        azimuth: 90,
        dip: -70,
        elevation: 405,
    },
];

const views: Array<[string, () => ReactElement]> = [
    [
        'Borehole3DView',
        () => (
            <Borehole3DView
                holes={[
                    {
                        collar_id: 'c1',
                        hole_id: 'DD-01',
                        total_depth: 600,
                        easting: 500000,
                        northing: 6000000,
                        lat: 54.0,
                        lng: -105.0,
                        bands: [{ from: 0, to: 600, code: 'SST', color: '#cccccc' }],
                    },
                ]}
                collars={collars}
            />
        ),
    ],
    [
        'AssayComposites3DView',
        () => (
            <AssayComposites3DView
                collars={collars}
                composites={[
                    {
                        collar_id: 'c1',
                        element: 'Au',
                        from_depth: 100,
                        to_depth: 110,
                        weighted_avg: 1.2,
                        unit: 'ppm',
                        cutoff_grade: null,
                        sample_count: 4,
                    },
                ]}
                elements={[{ element: 'Au', count: 1 }]}
            />
        ),
    ],
    [
        'CommoditySamples3DView',
        () => (
            <CommoditySamples3DView
                collars={collars}
                samples={[
                    {
                        collar_id: 'c1',
                        from_depth: 100,
                        to_depth: 101,
                        sample_type: 'core',
                        grades: { U3O8_pct_e: 0.1 },
                    },
                ]}
                commodityKeys={[{ key: 'U3O8_pct_e', count: 1 }]}
            />
        ),
    ],
    [
        'SignificantIntersections3DView',
        () => (
            <SignificantIntersections3DView
                collars={collars}
                intersections={[
                    {
                        collar_id: 'c1',
                        element: 'Au',
                        cutoff_grade: 0.5,
                        from_depth: 100,
                        to_depth: 110,
                        true_width_m: null,
                        weighted_avg: 2,
                        unit: 'ppm',
                        peak_value: 3,
                        peak_depth: 105,
                        zone_name: null,
                    },
                ]}
            />
        ),
    ],
    [
        'StructureDiscs3DView',
        () => (
            <StructureDiscs3DView
                collars={collars}
                structures={[
                    {
                        collar_id: 'c1',
                        strike_deg: 10,
                        dip_deg: 60,
                        measurement_kind: 'bedding',
                        depth_m: 120,
                        pole_trend_deg: 100,
                        pole_plunge_deg: 30,
                        display_color: null,
                        display_symbol: null,
                        confidence: null,
                    },
                ]}
            />
        ),
    ],
];

beforeEach(() => {
    plotted.layout = undefined;
});
afterEach(cleanup);

describe('Workspace 3D views: true scale', () => {
    it('shares one setting, and it is true scale', () => {
        expect(SCENE_3D_ASPECT).toEqual({ aspectmode: 'data' });
    });

    it.each(views)('%s draws 1 m the same length on every axis, with no forced box ratio', (_name, view) => {
        render(view());

        const scene = plotted.layout?.scene;
        expect(scene, 'the view produced a scene').toBeDefined();
        // Metres on all three axes...
        expect(scene?.zaxis?.title?.text).toMatch(/^Elevation \(m/);
        // ...drawn to scale: 'data', and no hand-picked aspectratio.
        expect(scene?.aspectmode).toBe('data');
        expect(scene).not.toHaveProperty('aspectratio');
    });
});
