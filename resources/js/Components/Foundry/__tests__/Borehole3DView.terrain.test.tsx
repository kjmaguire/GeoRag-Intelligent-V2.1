import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Borehole3DView } from '../Borehole3DView';

const plotted: { layout?: Record<string, unknown> } = {};

vi.mock('@/Components/GeoPlot', () => ({
    default: (props: { layout?: Record<string, unknown> }) => {
        plotted.layout = props.layout;

        return null;
    },
}));

const hole = {
    collar_id: 'c1',
    hole_id: 'DD-01',
    total_depth: 100,
    easting: 500000,
    northing: 6000000,
    lat: null,
    lng: null,
    bands: [{ from: 0, to: 100, code: 'SST', color: '#cccccc' }],
};

function collar(elevationSource: 'file' | 'terrain') {
    return {
        collar_id: 'c1',
        easting: 500000,
        northing: 6000000,
        total_depth: 100,
        elevation: 412.3,
        elevation_source: elevationSource,
        azimuth: 0,
        dip: -90,
    };
}

describe('Borehole3DView elevation caveat', () => {
    it('names the terrain model when a collar height came from it', () => {
        render(<Borehole3DView holes={[hole]} collars={[collar('terrain')]} />);

        expect(JSON.stringify(plotted.layout)).toContain('some from terrain model');
    });

    it('does not name the terrain model for file elevations', () => {
        render(<Borehole3DView holes={[hole]} collars={[collar('file')]} />);

        expect(JSON.stringify(plotted.layout)).not.toContain('some from terrain model');
    });
});
