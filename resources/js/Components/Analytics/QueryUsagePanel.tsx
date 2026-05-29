import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';

interface DailyPoint { day: string; c: number; }
interface TopQuery { q: string; c: number; }
interface Props {
    total30d: number;
    avgLatencyMs: number | null;
    daily: DailyPoint[];
    topQueries: TopQuery[];
}

/**
 * Platform-usage analytics for this project — what is actually being
 * asked about and how often. Useful for identifying:
 *   - Knowledge gaps (same question asked repeatedly)
 *   - Shift in focus over time (daily volume trend)
 *   - Performance regressions (avg latency vs. previous window)
 *
 * Deliberately scoped to the project via `project_id` in the audit
 * log query on the backend so cross-project noise doesn't pollute the
 * trend.
 */
export default function QueryUsagePanel({ total30d, avgLatencyMs, daily, topQueries }: Props) {
    const { trace, layout } = useMemo(() => {
        if (daily.length === 0) return { trace: null, layout: {} };
        return {
            trace: {
                type: 'bar',
                x: daily.map((d) => d.day),
                y: daily.map((d) => d.c),
                marker: { color: '#38bdf8' },
                hovertemplate: '%{x}<br>Queries: %{y}<extra></extra>',
            },
            layout: {
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                font: { color: '#94a3b8', size: 11 },
                margin: { l: 40, r: 12, t: 6, b: 40 },
                xaxis: { color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.18)' },
                yaxis: { color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.18)' },
                showlegend: false,
            },
        };
    }, [daily]);

    return (
        <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-4">
            <div className="bg-gray-900/40 rounded border border-gray-800 p-3">
                <div className="flex items-baseline justify-between mb-1">
                    <div className="text-xs text-gray-400">Queries over last 30 days</div>
                    <div className="text-[11px] text-gray-500 font-mono">
                        total {total30d} · avg {avgLatencyMs != null ? `${Math.round(avgLatencyMs)} ms` : '—'}
                    </div>
                </div>
                <div className="h-[240px]">
                    {trace ? (
                        <GeoPlot data={[trace] as unknown as Record<string, unknown>[]} layout={layout as Record<string, unknown>} />
                    ) : (
                        <div className="h-full flex items-center justify-center text-sm text-gray-500">No queries in the last 30 days.</div>
                    )}
                </div>
            </div>
            <div className="bg-gray-900/40 rounded border border-gray-800 p-3">
                <div className="text-xs text-gray-400 mb-2">Top queries (30 d)</div>
                {topQueries.length === 0 ? (
                    <div className="text-sm text-gray-500">—</div>
                ) : (
                    <ol className="space-y-1.5 text-[11px]">
                        {topQueries.map((q, i) => (
                            <li key={q.q + i} className="flex items-start gap-2 text-gray-300">
                                <span className="shrink-0 text-gray-500 font-mono w-8">×{q.c}</span>
                                <span className="truncate" title={q.q}>{q.q}</span>
                            </li>
                        ))}
                    </ol>
                )}
            </div>
        </div>
    );
}
