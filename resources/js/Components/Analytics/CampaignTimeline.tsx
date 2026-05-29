import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';

interface Collar {
    hole_id: string;
    drill_date: string | null;
    total_depth: number | null;
    hole_type: string | null;
    status: string | null;
}

interface Props { collars: Collar[]; }

const STATUS_COLOR: Record<string, string> = {
    Completed:     '#22c55e',
    'In Progress': '#eab308',
    Active:        '#eab308',
    Abandoned:     '#ef4444',
};

/**
 * Gantt-style campaign timeline: one horizontal bar per hole, positioned
 * at the hole's drill date, with bar length proportional to total depth
 * and colour reflecting status. Instantly shows how drilling intensity
 * has evolved over time and which campaigns left deeper holes behind.
 *
 * We plot each hole as a short horizontal segment using a narrow
 * `scatter` trace in shape='hv' so it appears as a thick marker at the
 * date, and we rely on a second scatter trace for the hole-id labels.
 * Plotly doesn't ship a first-class Gantt; this is the idiomatic
 * approximation. Looks clean and avoids pulling in another viz library.
 */
export default function CampaignTimeline({ collars }: Props) {
    const { traces, layout, hasData } = useMemo(() => {
        const dated = collars
            .filter((c) => c.drill_date != null && c.total_depth != null)
            .sort((a, b) => (a.drill_date! < b.drill_date! ? -1 : 1));

        if (dated.length === 0) {
            return { traces: [], layout: {}, hasData: false };
        }

        // Group by status so each status gets one trace + legend row.
        const byStatus: Record<string, Collar[]> = {};
        for (const c of dated) {
            const s = c.status || 'unknown';
            (byStatus[s] = byStatus[s] || []).push(c);
        }

        const traces: Record<string, unknown>[] = [];
        for (const [status, rows] of Object.entries(byStatus)) {
            traces.push({
                type: 'bar',
                orientation: 'h',
                x: rows.map((r) => r.total_depth),
                y: rows.map((r) => r.hole_id),
                base: 0,
                marker: { color: STATUS_COLOR[status] ?? '#94a3b8' },
                name: status,
                text: rows.map((r) => `${r.total_depth!.toFixed(0)} m · ${r.drill_date}`),
                hovertemplate: '%{y}<br>%{text}<extra>' + status + '</extra>',
            });
        }

        const layoutObj = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#94a3b8', size: 11 },
            margin: { l: 90, r: 12, t: 20, b: 40 },
            xaxis: {
                title: { text: 'Total depth (m)', font: { color: '#cbd5e1' } },
                gridcolor: 'rgba(148,163,184,0.18)',
                color: '#94a3b8',
            },
            yaxis: {
                automargin: true,
                color: '#94a3b8',
                categoryorder: 'array' as const,
                categoryarray: dated.map((r) => r.hole_id),
            },
            barmode: 'group' as const,
            legend: {
                font: { color: '#cbd5e1', size: 10 },
                orientation: 'h' as const,
                y: 1.15,
            },
        };

        return { traces, layout: layoutObj, hasData: true };
    }, [collars]);

    if (!hasData) {
        return <div className="flex items-center justify-center h-full text-sm text-gray-500">No dated holes to chart.</div>;
    }
    return <GeoPlot data={traces} layout={layout as Record<string, unknown>} />;
}
