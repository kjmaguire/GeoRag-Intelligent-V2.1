import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';

interface Row { date: string; hole_id: string; meters: number; cumulative: number; }
interface Props { rows: Row[]; }

/**
 * Cumulative meters drilled on the project, time-ordered. Each marker
 * is a hole; hover reveals hole_id + meters added + running total.
 * Useful as a "how much drilling has been done here?" glance tile that
 * sits above the detail panels.
 */
export default function CumulativeMetersStrip({ rows }: Props) {
    const { trace, layout } = useMemo(() => {
        if (rows.length === 0) return { trace: null, layout: {} };

        const x = rows.map((r) => r.date);
        const y = rows.map((r) => r.cumulative);
        const text = rows.map((r) => `${r.hole_id} · +${r.meters.toFixed(0)} m`);

        return {
            trace: {
                type: 'scatter',
                mode: 'lines+markers',
                x, y, text,
                line: { color: '#22d3ee', width: 2, shape: 'hv' },
                marker: { size: 6, color: '#22d3ee' },
                hovertemplate: 'Date: %{x}<br>%{text}<br>Cumulative: %{y:.0f} m<extra></extra>',
            },
            layout: {
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                font: { color: '#94a3b8', size: 11 },
                margin: { l: 52, r: 12, t: 6, b: 40 },
                xaxis: {
                    title: { text: 'Drill date', font: { color: '#cbd5e1' } },
                    gridcolor: 'rgba(148,163,184,0.18)', color: '#94a3b8',
                },
                yaxis: {
                    title: { text: 'Cumulative meters', font: { color: '#cbd5e1' } },
                    gridcolor: 'rgba(148,163,184,0.18)', color: '#94a3b8',
                },
                showlegend: false,
            },
        };
    }, [rows]);

    if (!trace) {
        return <div className="flex items-center justify-center h-full text-sm text-gray-500">No dated drill records.</div>;
    }
    return <GeoPlot data={[trace] as unknown as Record<string, unknown>[]} layout={layout as Record<string, unknown>} />;
}
