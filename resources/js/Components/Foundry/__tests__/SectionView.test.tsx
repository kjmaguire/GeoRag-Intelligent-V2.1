/**
 * SectionView — two holes read against each other, so they must be drawn on one
 * depth axis. The shared `depthMax` it computed was handed to a column that
 * ignored it, so a 130 m hole was stretched to the height of a 400 m one.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SectionView } from '../SectionView';

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

function payload(id: string, totalDepth: number) {
    return {
        hole_id: id,
        total_depth: totalDepth,
        easting: null,
        northing: null,
        lat: null,
        lng: null,
        lithology_intervals: [{ from: 0, to: totalDepth, code: id, label: id, color: '#8899aa' }],
        ore_bands: 0,
        ore_thickness_m: 0,
    };
}

describe('SectionView', () => {
    it('draws both holes to one scale, so a shallow hole stays shallow beside a deep one', async () => {
        vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
            const id = String(input).includes('/holes/SHALLOW/') ? 'SHALLOW' : 'DEEP';
            return new Response(JSON.stringify(payload(id, id === 'SHALLOW' ? 130 : 400)), { status: 200 });
        });

        render(
            <SectionView
                projectSlug="red-star"
                holeOptions={['SHALLOW', 'DEEP']}
                defaultLeft="SHALLOW"
                defaultRight="DEEP"
                chartH={300}
            />,
        );

        const shallow = await screen.findByLabelText('SHALLOW 0-130 m');
        const deep = await screen.findByLabelText('DEEP 0-400 m');
        // One axis to the deeper hole (400 m, rounded up to 500 m) over a 300 px plot.
        expect(Number(shallow.getAttribute('height'))).toBeCloseTo((130 / 500) * 300, 5);
        expect(Number(deep.getAttribute('height'))).toBeCloseTo((400 / 500) * 300, 5);
        // Depth 0 sits at the same y in both columns.
        expect(shallow.getAttribute('y')).toBe(deep.getAttribute('y'));
    });
});
