import { useMemo } from 'react';
import GeoPlot from './GeoPlot';

/**
 * TimelineCard — horizontal swimlane / Gantt-style coverage chart for the
 * `project_summary` intent (ADR-0007 PR-1).
 *
 * One row per `technique` (drill_type / survey_type / report_type).
 * X-axis is calendar year. Each bar spans [year_start, year_end] inclusive
 * and is annotated with the row's `count` (holes / surveys / reports).
 *
 * Honest gap rendering (§04i — "the refusal path is the product"): when
 * `contractor` / `geologist` is null we render "— (not extracted yet)" in
 * the hover tooltip rather than hiding the dimension. PR-3 backfills the
 * NER pipeline that populates these columns.
 */

export interface TimelineSwimlane {
    technique: string;
    year_start: number;
    year_end: number;
    count: number;
    total_metres?: number | null;
    contractor?: string | null;
    geologist?: string | null;
    source_row_ids?: string[];
}

interface TimelineCardProps {
    swimlanes: TimelineSwimlane[];
    title?: string;
}

// Stable, accessible palette — keep parallel with InlineViz dark theme.
const LANE_COLORS = [
    '#f59e0b', // amber
    '#22c55e', // green
    '#3b82f6', // blue
    '#a855f7', // violet
    '#ef4444', // red
    '#06b6d4', // cyan
    '#ec4899', // pink
    '#eab308', // yellow
];

function formatField(value: string | null | undefined): string {
    if (value === null || value === undefined || value === '') {
        return '— (not extracted yet)';
    }
    return value;
}

export default function TimelineCard({ swimlanes, title }: TimelineCardProps) {
    const { data, layout } = useMemo(() => {
        if (!swimlanes || swimlanes.length === 0) {
            return { data: [] as Record<string, unknown>[], layout: {} as Record<string, unknown> };
        }

        // Build one bar trace per lane so we can colour them distinctly and
        // attach custom hover text per row.
        const techniques = swimlanes.map((s) => s.technique);

        const traces: Record<string, unknown>[] = swimlanes.map((lane, idx) => {
            const startYear = lane.year_start;
            const endYear = lane.year_end;
            // Plotly bar with `base` + `x` (width) renders a horizontal span.
            // Add a 1-year pad so single-year campaigns are visible as a bar.
            const span = Math.max(endYear - startYear + 1, 1);

            const hover =
                `<b>${lane.technique}</b><br>` +
                `Years: ${startYear}–${endYear}<br>` +
                `Count: ${lane.count.toLocaleString()}` +
                (lane.total_metres != null
                    ? `<br>Total metres: ${lane.total_metres.toLocaleString()}`
                    : '') +
                `<br>Contractor: ${formatField(lane.contractor)}` +
                `<br>Geologist: ${formatField(lane.geologist)}` +
                '<extra></extra>';

            return {
                type: 'bar',
                orientation: 'h',
                x: [span],
                base: [startYear],
                y: [lane.technique],
                width: [0.55],
                marker: {
                    color: LANE_COLORS[idx % LANE_COLORS.length],
                    line: { width: 0 },
                },
                text: [`${lane.count.toLocaleString()}`],
                textposition: 'inside',
                insidetextanchor: 'middle',
                textfont: { color: '#0b0f19', size: 11 },
                hovertemplate: hover,
                showlegend: false,
                name: lane.technique,
            };
        });

        const allYears = swimlanes.flatMap((s) => [s.year_start, s.year_end]);
        const minYear = Math.min(...allYears) - 1;
        const maxYear = Math.max(...allYears) + 1;

        const layout: Record<string, unknown> = {
            barmode: 'overlay',
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            margin: { l: 140, r: 24, t: 8, b: 36 },
            xaxis: {
                title: { text: 'Year', font: { color: '#9ca3af', size: 11 } },
                range: [minYear, maxYear],
                tickfont: { color: '#9ca3af', size: 10 },
                gridcolor: '#1f2937',
                zerolinecolor: '#1f2937',
                tickformat: 'd',
            },
            yaxis: {
                automargin: true,
                categoryorder: 'array',
                categoryarray: [...techniques].reverse(),
                tickfont: { color: '#e5e7eb', size: 11 },
                gridcolor: '#111827',
            },
            hoverlabel: {
                bgcolor: '#0f172a',
                bordercolor: '#374151',
                font: { color: '#e5e7eb', size: 11 },
            },
        };

        return { data: traces, layout };
    }, [swimlanes]);

    if (!swimlanes || swimlanes.length === 0) {
        return (
            <div
                className="flex items-center justify-center h-full text-xs text-gray-500"
                data-testid="timeline-empty"
            >
                No timeline data to display.
            </div>
        );
    }

    return (
        <div className="w-full h-full" data-testid="timeline-card" aria-label={title || 'Technique timeline'}>
            <GeoPlot data={data} layout={layout} />
        </div>
    );
}
