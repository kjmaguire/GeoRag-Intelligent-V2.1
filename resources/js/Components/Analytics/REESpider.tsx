import { useMemo, useState } from 'react';
import GeoPlot from '../GeoPlot';

interface GeochemRow {
    hole_id: string;
    from_depth: number;
    to_depth: number;
    ree_json: Record<string, number> | string | null;
}

interface Props { rows: GeochemRow[]; }

/**
 * Chondrite-normalised rare-earth element (REE) spider plot.
 *
 * Each geochem sample in the project renders as a thin line across
 * the REE axis. The X-axis walks La → Lu in standard atomic-number
 * order; Y is the sample's normalised value (values in the ree_json
 * blob are already chondrite-normalised during the seeder / Dagster
 * silver transform). Samples are semi-transparent so the dominant
 * pattern emerges as a visible envelope; the median line (dashed white)
 * summarises the project fingerprint.
 */

// Atomic number order — standard x-axis for REE spider plots.
const REE_ORDER = ['La', 'Ce', 'Pr', 'Nd', 'Sm', 'Eu', 'Gd', 'Tb', 'Dy', 'Ho', 'Er', 'Tm', 'Yb', 'Lu'];

function parseRee(raw: GeochemRow['ree_json']): Record<string, number> | null {
    if (!raw) return null;
    if (typeof raw === 'object') return raw as Record<string, number>;
    try { return JSON.parse(raw); } catch { return null; }
}

export default function REESpider({ rows }: Props) {
    const [holeFilter, setHoleFilter] = useState<string>('__all__');

    const holeIds = useMemo(
        () => Array.from(new Set(rows.map((r) => r.hole_id))).sort(),
        [rows],
    );

    const { traces, layout, sampleCount } = useMemo(() => {
        const filtered = holeFilter === '__all__' ? rows : rows.filter((r) => r.hole_id === holeFilter);

        // Build sample × element matrix.
        const series: { values: (number | null)[] }[] = [];
        for (const r of filtered) {
            const ree = parseRee(r.ree_json);
            if (!ree) continue;
            const values = REE_ORDER.map((el) => {
                // Accept `La_N`, `La`, `la_n`, `la` — match whichever the blob uses.
                const v = ree[`${el}_N`] ?? ree[el] ?? ree[`${el.toLowerCase()}_n`] ?? ree[el.toLowerCase()];
                return typeof v === 'number' && Number.isFinite(v) ? v : null;
            });
            if (values.some((v) => v !== null)) {
                series.push({ values });
            }
        }

        if (series.length === 0) {
            return { traces: [] as Record<string, unknown>[], layout: {}, sampleCount: 0 };
        }

        // Per-element median to overlay.
        const medians = REE_ORDER.map((_, i) => {
            const vs = series.map((s) => s.values[i]).filter((v): v is number => v !== null).sort((a, b) => a - b);
            if (vs.length === 0) return null;
            return vs[Math.floor(vs.length / 2)];
        });

        const traces: Record<string, unknown>[] = series.map((s, idx) => ({
            type: 'scatter',
            mode: 'lines',
            x: REE_ORDER,
            y: s.values,
            line: { color: 'rgba(96, 165, 250, 0.25)', width: 1 },
            hoverinfo: idx === 0 ? undefined : 'skip',
            showlegend: false,
        }));

        traces.push({
            type: 'scatter',
            mode: 'lines+markers',
            x: REE_ORDER,
            y: medians,
            line: { color: '#f59e0b', width: 2.5, dash: 'dash' },
            marker: { color: '#f59e0b', size: 6 },
            name: `Median (${series.length} samples)`,
            hovertemplate: 'Element: %{x}<br>Median: %{y:.1f}<extra></extra>',
            showlegend: true,
        });

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#94a3b8', size: 11 },
            margin: { l: 60, r: 12, t: 16, b: 40 },
            xaxis: {
                title: { text: 'Rare earth element (atomic number order)', font: { color: '#cbd5e1' } },
                type: 'category' as const,
                color: '#94a3b8',
                gridcolor: 'rgba(148,163,184,0.18)',
            },
            yaxis: {
                title: { text: 'Chondrite-normalised value', font: { color: '#cbd5e1' } },
                type: 'log' as const,
                color: '#94a3b8',
                gridcolor: 'rgba(148,163,184,0.18)',
            },
            legend: { font: { color: '#cbd5e1', size: 10 } },
        };

        return { traces, layout, sampleCount: series.length };
    }, [rows, holeFilter]);

    return (
        <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
                <label className="text-xs text-gray-400">Hole</label>
                <select
                    value={holeFilter}
                    onChange={(e) => setHoleFilter(e.target.value)}
                    className="bg-gray-900 border border-gray-700 rounded text-xs text-gray-200 px-2 py-1"
                >
                    <option value="__all__">All holes ({holeIds.length})</option>
                    {holeIds.map((h) => <option key={h} value={h}>{h}</option>)}
                </select>
                <span className="text-[11px] text-gray-500 ml-2">
                    {sampleCount} sample{sampleCount === 1 ? '' : 's'}
                </span>
                <span className="ml-auto text-[11px] text-gray-500">
                    Values are chondrite-normalised. Log-Y axis.
                </span>
            </div>
            {sampleCount === 0 ? (
                <div className="h-[320px] flex items-center justify-center text-sm text-gray-500">
                    No REE data for the current filter.
                </div>
            ) : (
                <div className="h-[360px]">
                    <GeoPlot data={traces} layout={layout as Record<string, unknown>} />
                </div>
            )}
        </div>
    );
}
