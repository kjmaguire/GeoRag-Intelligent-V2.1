/**
 * The strip log draws lithology, alteration and mineralization.
 *
 * The hole page drew every gold row on top of the others in one column, filled
 * with `color_hint` (colour TEXT or a rock code - not a colour), and the
 * Workspace LOGS column drew lithology only. Alteration and mineralization were
 * drawn nowhere. These tests pin what a geologist should see per track, and
 * that a described colour never becomes a fill.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import DrillholeStripLog from '../Foundry/DrillholeStripLog';
import { LithologyStripColumn } from '../Foundry/Charts';
import StripLogViewer from '../StripLogViewer';
import { lithologyColour, type StripTracks } from '../../lib/stripLog';

const tracks: StripTracks = {
    lithology: [
        {
            from: 0,
            to: 5,
            code: 'GRN',
            label: 'Grey granite',
            color: '#8899aa',
            detail: { description: 'Grey granite', colour: 'dark grey', grain_size: 'Fine', rqd: 85, recovery: 98 },
        },
        { from: 5, to: 10, code: 'SST', label: 'Sandstone', color: '' },
    ],
    alteration: [
        {
            from: 0,
            to: 5,
            label: 'Chlorite (Strong); Silica',
            alterations: [
                { type: 'Chlorite', intensity: 'Strong', minerals: ['chlorite'], notes: null },
                { type: 'Silica', intensity: null, minerals: [], notes: null },
            ],
        },
    ],
    mineralization: [
        { from: 5, to: 10, mineral: 'Pyrite', abundance_pct: 3, form: 'Disseminated', grain_size: null, notes: null },
        {
            from: 5,
            to: 10,
            mineral: 'Chalcopyrite',
            abundance_pct: null,
            form: null,
            grain_size: null,
            notes: 'abundance: trace',
        },
    ],
    truncated: { lithology: false, alteration: false, mineralization: false },
};

describe('DrillholeStripLog', () => {
    it('draws one column per kind of logging', () => {
        render(<DrillholeStripLog tracks={tracks} maxDepth={20} />);

        const svg = screen.getByRole('img', { name: 'Strip log' });
        expect(within(svg).getByLabelText('Lithology')).toBeTruthy();
        expect(within(svg).getByLabelText('Alteration')).toBeTruthy();
        expect(within(svg).getByLabelText('Mineralization')).toBeTruthy();
        expect(within(svg).getByText('LITHOLOGY')).toBeTruthy();
        expect(within(svg).getByText('ALTERATION')).toBeTruthy();
        expect(within(svg).getByText('MINERALIZATION')).toBeTruthy();
    });

    it('colours lithology by the data hex colour, else by a stable colour for the code', () => {
        render(<DrillholeStripLog tracks={tracks} maxDepth={20} />);

        const grn = screen.getByLabelText('GRN 0-5 m');
        const sst = screen.getByLabelText('SST 5-10 m');
        expect(grn.getAttribute('fill')).toBe('#8899aa');
        expect(sst.getAttribute('fill')).toBe(lithologyColour('SST'));
        // A described colour ("dark grey") never becomes a fill.
        expect(grn.getAttribute('fill')).not.toBe('dark grey');
    });

    it('writes the mineral and its percentage in the band, and only a real percentage', () => {
        render(<DrillholeStripLog tracks={tracks} maxDepth={20} />);

        expect(screen.getByLabelText('Pyrite 3% 5-10 m')).toBeTruthy();
        // No percentage was logged for chalcopyrite: none is invented.
        expect(screen.getByLabelText('Chalcopyrite 5-10 m')).toBeTruthy();
        expect(screen.queryByLabelText(/Chalcopyrite \d+(\.\d+)?%/)).toBeNull();
    });

    it('splits an interval with two alterations into two segments', () => {
        render(<DrillholeStripLog tracks={tracks} maxDepth={20} />);

        const band = screen.getByLabelText('Alteration Chlorite (Strong); Silica 0-5 m');
        expect(band.querySelectorAll('rect').length).toBe(2);
        expect(band.textContent).toContain('Chlorite (Strong)');
    });

    it('shows the description and attributes when a band is clicked', () => {
        render(<DrillholeStripLog tracks={tracks} maxDepth={20} />);
        expect(screen.queryByRole('status', { name: 'Selected interval' })).toBeNull();

        fireEvent.click(screen.getByLabelText('GRN 0-5 m'));

        const detail = screen.getByRole('status', { name: 'Selected interval' });
        expect(detail.textContent).toContain('GRN  0-5 m');
        expect(detail.textContent).toContain('Grey granite');
        expect(detail.textContent).toContain('Colour: dark grey');
        expect(detail.textContent).toContain('Grain size: Fine');
        expect(detail.textContent).toContain('RQD: 85%');

        fireEvent.click(screen.getByLabelText('Clear selection'));
        expect(screen.queryByRole('status', { name: 'Selected interval' })).toBeNull();
    });

    it('puts the description on every band as a hover title', () => {
        render(<DrillholeStripLog tracks={tracks} maxDepth={20} />);

        const title = screen.getByLabelText('GRN 0-5 m').querySelector('title');
        expect(title?.textContent).toContain('Grey granite');
        expect(title?.textContent).toContain('Grain size: Fine');
    });

    it('lists what each colour means', () => {
        render(<DrillholeStripLog tracks={tracks} maxDepth={20} />);

        expect(screen.getByLabelText('Lithology legend').textContent).toContain('GRN');
        expect(screen.getByLabelText('Alteration legend').textContent).toContain('Chlorite');
        expect(screen.getByLabelText('Alteration legend').textContent).toContain('Silica');
        expect(screen.getByLabelText('Minerals legend').textContent).toContain('Pyrite');
    });

    it('draws no alteration or mineralization column for a hole that has none', () => {
        render(<DrillholeStripLog tracks={{ ...tracks, alteration: [], mineralization: [] }} maxDepth={20} />);

        expect(screen.queryByText('ALTERATION')).toBeNull();
        expect(screen.queryByText('MINERALIZATION')).toBeNull();
        expect(screen.queryByLabelText('Alteration legend')).toBeNull();
    });

    it('says when the server cut a track at its bound', () => {
        render(
            <DrillholeStripLog
                tracks={{ ...tracks, truncated: { lithology: false, alteration: false, mineralization: true } }}
                maxDepth={20}
            />,
        );
        expect(screen.getByRole('note').textContent).toContain('mineralization');
    });

    it('draws the sampled windows with their values on hover', () => {
        render(
            <DrillholeStripLog
                tracks={tracks}
                maxDepth={20}
                sampleWindows={[{ depth_from: 2, depth_to: 3, assay_payload: { Au_ppm: 1.5 } }]}
            />,
        );
        const sample = screen.getByLabelText('Sample 2-3 m');
        expect(sample.querySelector('title')?.textContent).toContain('Au_ppm: 1.5');
    });
});

describe('LithologyStripColumn (Workspace LOGS)', () => {
    it('draws alteration and mineralization beside the lithology', () => {
        render(
            <LithologyStripColumn
                intervals={tracks.lithology}
                alteration={tracks.alteration}
                mineralization={tracks.mineralization}
                holeId="H1"
                depthMax={20}
                height={400}
                width={520}
            />,
        );

        expect(screen.getByText('ALTERATION')).toBeTruthy();
        expect(screen.getByText('MINERALS')).toBeTruthy();
        expect(screen.getByLabelText('Pyrite 3% 5-10 m')).toBeTruthy();
        expect(screen.getByLabelText('GRN 0-5 m').getAttribute('fill')).toBe('#8899aa');
    });

    it('still draws when a hole has alteration but no lithology', () => {
        render(
            <LithologyStripColumn
                intervals={[]}
                alteration={tracks.alteration}
                holeId="H1"
                depthMax={20}
                height={400}
                width={520}
            />,
        );
        expect(screen.getByLabelText('Alteration Chlorite (Strong); Silica 0-5 m')).toBeTruthy();
    });

    it('says so when nothing is logged at all', () => {
        render(<LithologyStripColumn intervals={[]} holeId="H1" depthMax={20} />);
        expect(screen.getByText('No lithology logged for this hole.')).toBeTruthy();
    });

    it('does not fill a band with colour text or a rock code left in color', () => {
        render(
            <LithologyStripColumn
                intervals={[{ from: 0, to: 5, code: 'GRN', label: 'x', color: 'dark grey' }]}
                holeId="H1"
                depthMax={20}
            />,
        );
        expect(screen.getByLabelText('GRN 0-5 m').getAttribute('fill')).toBe(lithologyColour('GRN'));
    });
});

describe('StripLogViewer (inline in chat)', () => {
    afterEach(() => vi.restoreAllMocks());

    const collar = {
        collar_id: 'c1',
        hole_id: 'DH-9',
        project_id: 'p1',
        total_depth: 0,
        azimuth: 0,
        dip: -60,
        lithology_logs: [
            {
                log_id: 'a',
                from_depth: 0,
                to_depth: 5,
                lithology_code: 'QZ-MON',
                lithology_description: 'Quartz monzonite',
                color: '#334455',
                rqd: 80,
                recovery: 95,
            },
            {
                log_id: 'b',
                from_depth: 5,
                to_depth: 12,
                lithology_code: 'SST',
                lithology_description: 'Sandstone',
                color: 'red',
            },
        ],
        alterations: [
            {
                alteration_id: 'x',
                from_depth: 0,
                to_depth: 5,
                alteration_type: 'Chlorite',
                intensity: 'Strong',
                minerals: [],
                notes: null,
            },
        ],
        mineralization: [
            {
                mineralization_id: 'm',
                from_depth: 5,
                to_depth: 12,
                mineral: 'Pyrite',
                abundance_pct: 3,
                form: null,
                grain_size: null,
                notes: null,
            },
        ],
        well_log_curves: [],
    };

    function mockFetch() {
        return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
            const url = String(input);
            const body = url.includes('/collars/c1')
                ? { data: collar }
                : { data: [{ collar_id: 'c1', hole_id: 'DH-9' }] };
            return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
        });
    }

    it('draws alteration and mineralization tracks and labels the columns', async () => {
        mockFetch();
        render(<StripLogViewer holeId="DH-9" projectId="p1" />);

        await waitFor(() => expect(screen.getByRole('img', { name: /Strip log for drill hole DH-9/ })).toBeTruthy());
        expect(screen.getByLabelText('Alteration')).toBeTruthy();
        expect(screen.getByLabelText('Mineralization')).toBeTruthy();
        expect(screen.getByLabelText('Pyrite 3% 5-12 m')).toBeTruthy();
    });

    it('colours any lithology code, not only four hard-coded ones, and legends it from the data', async () => {
        mockFetch();
        render(<StripLogViewer holeId="DH-9" projectId="p1" />);

        const band = await screen.findByLabelText('QZ-MON 0–5 m');
        // The log's own hex colour wins; an unlisted code is not the generic gray.
        expect(band.getAttribute('fill')).toBe('#334455');
        const sst = screen.getByLabelText('SST 5–12 m');
        expect(sst.getAttribute('fill')).toBe(lithologyColour('SST'));
        expect(sst.getAttribute('fill')).not.toBe('#6b7280');

        const legend = screen.getByText('Legend').parentElement as HTMLElement;
        expect(legend.textContent).toContain('QZ-MON');
        expect(legend.textContent).toContain('Quartz monzonite');
    });

    it('scales to the logged depth when the collar carries no total depth', async () => {
        mockFetch();
        render(<StripLogViewer holeId="DH-9" projectId="p1" />);

        await screen.findByLabelText('QZ-MON 0–5 m');
        const rect = screen.getByLabelText('SST 5–12 m');
        // total_depth 0 used to divide by zero (NaN y / height).
        expect(rect.getAttribute('y')).not.toContain('NaN');
        expect(rect.getAttribute('height')).not.toContain('NaN');
    });
});
