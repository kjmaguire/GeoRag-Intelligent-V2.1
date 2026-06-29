/**
 * TimelineCard.test.tsx
 *
 * Coverage:
 *   - Empty state when swimlanes is missing/empty
 *   - Renders one Plotly trace per swimlane (including a null-contractor row)
 *   - Honest gap rendering: null contractor/geologist surface as
 *     "— (not extracted yet)" in the hover template (§04i refusal-path UX)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// Mock GeoPlot to capture the Plotly data/layout without booting jsdom
// down the WebGL path. The mock renders a probe div with serialized
// hovertemplates so assertions can introspect tooltip content.
vi.mock('../GeoPlot', () => ({
    default: ({ data, layout }: { data: any[]; layout: any }) => (
        <div data-testid="geoplot-mock">
            <div data-testid="trace-count">{data.length}</div>
            <div data-testid="layout-bar-mode">{(layout as any)?.barmode ?? ''}</div>
            {data.map((trace, idx) => (
                <div
                    key={idx}
                    data-testid={`trace-${idx}`}
                    data-name={trace.name}
                    data-hover={trace.hovertemplate}
                />
            ))}
        </div>
    ),
}));

import TimelineCard, { type TimelineSwimlane } from '../TimelineCard';

const SWIMLANES: TimelineSwimlane[] = [
    {
        technique: 'Diamond drill',
        year_start: 2018,
        year_end: 2021,
        count: 42,
        total_metres: 12500,
        contractor: 'Major Drilling',
        geologist: 'A. Geologist',
        source_row_ids: ['row-1', 'row-2'],
    },
    {
        technique: 'IP survey',
        year_start: 2019,
        year_end: 2019,
        count: 3,
        // contractor intentionally null → must render gap message
        contractor: null,
        geologist: null,
        source_row_ids: ['row-3'],
    },
    {
        technique: 'NI 43-101 report',
        year_start: 2020,
        year_end: 2024,
        count: 5,
        contractor: null,
        geologist: undefined,
        source_row_ids: ['row-4'],
    },
];

describe('TimelineCard — empty guard', () => {
    it('renders an empty state when swimlanes is empty', () => {
        render(<TimelineCard swimlanes={[]} />);
        expect(screen.getByTestId('timeline-empty')).toBeDefined();
    });

    it('renders an empty state when swimlanes is null-ish', () => {
        // @ts-expect-error — intentionally testing degenerate input
        render(<TimelineCard swimlanes={null} />);
        expect(screen.getByTestId('timeline-empty')).toBeDefined();
    });
});

describe('TimelineCard — rendering', () => {
    beforeEach(() => {
        render(<TimelineCard swimlanes={SWIMLANES} title="Project A timeline" />);
    });

    it('mounts the GeoPlot wrapper', () => {
        expect(screen.getByTestId('geoplot-mock')).toBeDefined();
    });

    it('emits one trace per swimlane', () => {
        expect(screen.getByTestId('trace-count').textContent).toBe('3');
    });

    it('uses barmode=overlay so horizontal lanes stack independently', () => {
        expect(screen.getByTestId('layout-bar-mode').textContent).toBe('overlay');
    });

    it('preserves the technique name on each trace', () => {
        expect(screen.getByTestId('trace-0').getAttribute('data-name')).toBe('Diamond drill');
        expect(screen.getByTestId('trace-1').getAttribute('data-name')).toBe('IP survey');
        expect(screen.getByTestId('trace-2').getAttribute('data-name')).toBe('NI 43-101 report');
    });

    it('renders contractor/geologist values when populated', () => {
        const hover = screen.getByTestId('trace-0').getAttribute('data-hover') ?? '';
        expect(hover).toContain('Major Drilling');
        expect(hover).toContain('A. Geologist');
    });

    it('renders honest gap text when contractor is null', () => {
        const hover = screen.getByTestId('trace-1').getAttribute('data-hover') ?? '';
        expect(hover).toContain('Contractor: — (not extracted yet)');
        expect(hover).toContain('Geologist: — (not extracted yet)');
    });

    it('renders honest gap text when geologist is undefined', () => {
        const hover = screen.getByTestId('trace-2').getAttribute('data-hover') ?? '';
        expect(hover).toContain('Geologist: — (not extracted yet)');
    });

    it('includes total_metres in the hover when provided', () => {
        const hover = screen.getByTestId('trace-0').getAttribute('data-hover') ?? '';
        expect(hover).toContain('Total metres: 12,500');
    });

    it('omits total_metres from the hover when not provided', () => {
        const hover = screen.getByTestId('trace-1').getAttribute('data-hover') ?? '';
        expect(hover).not.toContain('Total metres');
    });
});
