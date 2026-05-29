import { useMemo, useState } from 'react';
import GeoPlot from '../GeoPlot';

interface GeochemRow {
    hole_id: string;
    from_depth: number;
    to_depth: number;
    sio2_wt_pct: number | null;
    al2o3_wt_pct: number | null;
    fe2o3_wt_pct: number | null;
    mgo_wt_pct: number | null;
    cao_wt_pct: number | null;
    na2o_wt_pct: number | null;
    k2o_wt_pct: number | null;
    mg_number: number | null;
    cia: number | null;
    eu_anomaly: number | null;
}

interface Props { rows: GeochemRow[]; }

/**
 * Project-wide distribution of a selected geochemical variable. Shows
 * a histogram and a cumulative distribution curve side by side so the
 * user can read both the "where do most samples sit?" story (histogram
 * mode/skew) and the "what percentile is my sample at?" story (CDF).
 *
 * Dropdown picks the variable. Optional hole filter narrows the sample
 * set without forcing the user to scan each hole's per-hole Geochem tab.
 */

type Field =
    | 'sio2_wt_pct'
    | 'al2o3_wt_pct'
    | 'fe2o3_wt_pct'
    | 'mgo_wt_pct'
    | 'cao_wt_pct'
    | 'k2o_wt_pct'
    | 'mg_number'
    | 'cia'
    | 'eu_anomaly';

const FIELD_LABELS: Record<Field, string> = {
    sio2_wt_pct:  'SiO₂ (wt%)',
    al2o3_wt_pct: 'Al₂O₃ (wt%)',
    fe2o3_wt_pct: 'Fe₂O₃ (wt%)',
    mgo_wt_pct:   'MgO (wt%)',
    cao_wt_pct:   'CaO (wt%)',
    k2o_wt_pct:   'K₂O (wt%)',
    mg_number:    'Mg#',
    cia:          'Chemical Index of Alteration (CIA)',
    eu_anomaly:   'Eu/Eu*',
};

export default function GradeDistribution({ rows }: Props) {
    const [field, setField] = useState<Field>('cia');
    const [holeFilter, setHoleFilter] = useState<string>('__all__');

    const holeIds = useMemo(
        () => Array.from(new Set(rows.map((r) => r.hole_id))).sort(),
        [rows],
    );

    const values = useMemo(() => {
        const filtered = holeFilter === '__all__' ? rows : rows.filter((r) => r.hole_id === holeFilter);
        return filtered
            .map((r) => r[field])
            .filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
    }, [rows, field, holeFilter]);

    const stats = useMemo(() => {
        if (values.length === 0) return null;
        const sorted = [...values].sort((a, b) => a - b);
        const mean = sorted.reduce((s, v) => s + v, 0) / sorted.length;
        const p = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
        return {
            n: sorted.length,
            min: sorted[0],
            max: sorted[sorted.length - 1],
            mean,
            median: p(0.5),
            p25: p(0.25),
            p75: p(0.75),
        };
    }, [values]);

    const histTrace = useMemo(() => ({
        type: 'histogram',
        x: values,
        marker: { color: '#60a5fa', line: { color: '#1e3a8a', width: 0.5 } },
        opacity: 0.9,
        nbinsx: 20,
        hovertemplate: 'Range: %{x}<br>Count: %{y}<extra></extra>',
    }), [values]);

    const cdfTrace = useMemo(() => {
        if (values.length === 0) return null;
        const sorted = [...values].sort((a, b) => a - b);
        const x = sorted;
        const y = sorted.map((_, i) => ((i + 1) / sorted.length) * 100);
        return {
            type: 'scatter',
            mode: 'lines',
            x, y,
            line: { color: '#a855f7', width: 2 },
            hovertemplate: `${FIELD_LABELS[field]}: %{x:.2f}<br>Percentile: %{y:.1f}%<extra></extra>`,
        };
    }, [values, field]);

    const histLayout = {
        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#94a3b8', size: 11 },
        margin: { l: 52, r: 12, t: 6, b: 40 },
        xaxis: { title: { text: FIELD_LABELS[field], font: { color: '#cbd5e1' } }, gridcolor: 'rgba(148,163,184,0.18)', color: '#94a3b8' },
        yaxis: { title: { text: 'Samples', font: { color: '#cbd5e1' } }, gridcolor: 'rgba(148,163,184,0.18)', color: '#94a3b8' },
        showlegend: false,
        bargap: 0.05,
    };

    const cdfLayout = {
        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#94a3b8', size: 11 },
        margin: { l: 52, r: 12, t: 6, b: 40 },
        xaxis: { title: { text: FIELD_LABELS[field], font: { color: '#cbd5e1' } }, gridcolor: 'rgba(148,163,184,0.18)', color: '#94a3b8' },
        yaxis: { title: { text: 'Cumulative %', font: { color: '#cbd5e1' } }, range: [0, 100], gridcolor: 'rgba(148,163,184,0.18)', color: '#94a3b8' },
        showlegend: false,
    };

    return (
        <div className="space-y-3">
            <div className="flex flex-wrap gap-2 items-center">
                <label className="text-xs text-gray-400">Variable</label>
                <select
                    value={field}
                    onChange={(e) => setField(e.target.value as Field)}
                    className="bg-gray-900 border border-gray-700 rounded text-xs text-gray-200 px-2 py-1"
                >
                    {Object.entries(FIELD_LABELS).map(([k, v]) => (
                        <option key={k} value={k}>{v}</option>
                    ))}
                </select>

                <label className="text-xs text-gray-400 ml-3">Hole</label>
                <select
                    value={holeFilter}
                    onChange={(e) => setHoleFilter(e.target.value)}
                    className="bg-gray-900 border border-gray-700 rounded text-xs text-gray-200 px-2 py-1"
                >
                    <option value="__all__">All holes ({holeIds.length})</option>
                    {holeIds.map((h) => <option key={h} value={h}>{h}</option>)}
                </select>

                {stats && (
                    <div className="ml-auto text-[11px] text-gray-400 font-mono">
                        n={stats.n} · mean={stats.mean.toFixed(2)} · median={stats.median.toFixed(2)} ·
                        p25–p75={stats.p25.toFixed(2)}–{stats.p75.toFixed(2)} · min={stats.min.toFixed(2)} · max={stats.max.toFixed(2)}
                    </div>
                )}
            </div>

            {values.length === 0 ? (
                <div className="h-[320px] flex items-center justify-center text-sm text-gray-500">
                    No samples have a valid value for {FIELD_LABELS[field]}.
                </div>
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="bg-gray-900/40 rounded border border-gray-800 p-3">
                        <div className="text-xs text-gray-400 mb-1">Histogram</div>
                        <div className="h-[280px]">
                            <GeoPlot data={[histTrace] as unknown as Record<string, unknown>[]} layout={histLayout as Record<string, unknown>} />
                        </div>
                    </div>
                    <div className="bg-gray-900/40 rounded border border-gray-800 p-3">
                        <div className="text-xs text-gray-400 mb-1">Cumulative distribution</div>
                        <div className="h-[280px]">
                            {cdfTrace && (
                                <GeoPlot data={[cdfTrace] as unknown as Record<string, unknown>[]} layout={cdfLayout as Record<string, unknown>} />
                            )}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
