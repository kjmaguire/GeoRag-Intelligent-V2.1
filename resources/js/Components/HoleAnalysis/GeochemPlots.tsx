import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';

interface GeochemRow {
    from_depth: number;
    to_depth: number;
    sio2_wt_pct: number | null;
    al2o3_wt_pct: number | null;
    fe2o3_wt_pct: number | null;
    mgo_wt_pct: number | null;
    mg_number: number | null;
    cia: number | null;
    eu_anomaly: number | null;
    ree_json: Record<string, number> | string | null;
}

interface GeochemPlotsProps {
    rows: GeochemRow[];
    holeId: string;
}

/**
 * Four-panel petrology cross-plot set matching the reference image:
 *
 *   1. Mg# vs SiO₂            — igneous differentiation indicator
 *   2. Eu/Eu* vs SiO₂         — plagioclase fractionation signature
 *   3. CIA vs Depth           — chemical weathering index down the hole
 *   4. (La/Yb)_N vs Depth     — light-vs-heavy REE fractionation
 *
 * All plots share the hole's depth axis where relevant so geologists can
 * line them up against the strip log. Missing values are dropped per-plot
 * rather than dropping the whole row — e.g. a sample with valid SiO₂ but
 * a NULL Eu anomaly still shows up in the Mg# plot.
 */
export default function GeochemPlots({ rows, holeId }: GeochemPlotsProps) {
    const plots = useMemo(() => buildPlots(rows), [rows]);

    if (rows.length === 0) {
        return (
            <div className="flex items-center justify-center h-full min-h-[280px] text-gray-500 text-sm">
                No geochemistry rows logged for {holeId}.
            </div>
        );
    }

    return (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 w-full">
            {plots.map((p) => (
                <div key={p.title} className="bg-gray-900/40 rounded border border-gray-800 p-3 min-h-[280px]">
                    <div className="text-xs font-medium text-gray-300 mb-1">{p.title}</div>
                    <div className="text-[10px] text-gray-500 mb-2">{p.subtitle}</div>
                    <div className="h-[240px]">
                        {p.data.length > 0 ? (
                            <GeoPlot data={[p.trace] as unknown as Record<string, unknown>[]} layout={p.layout} />
                        ) : (
                            <div className="h-full flex items-center justify-center text-gray-500 text-xs">
                                No valid data for this plot.
                            </div>
                        )}
                    </div>
                </div>
            ))}
        </div>
    );
}

// ─── Plot builders ────────────────────────────────────────────────────────

function buildPlots(rows: GeochemRow[]) {
    const depthOf = (r: GeochemRow) => 0.5 * (r.from_depth + r.to_depth);

    // Parse REE JSON once (backend may send either object or serialised string).
    const reeFor = (r: GeochemRow): Record<string, number> | null => {
        if (!r.ree_json) return null;
        if (typeof r.ree_json === 'object') return r.ree_json as Record<string, number>;
        try { return JSON.parse(r.ree_json); } catch { return null; }
    };

    // Plot 1: Mg# vs SiO₂
    const p1 = rows
        .filter((r) => r.sio2_wt_pct != null && r.mg_number != null)
        .map((r) => ({ x: r.sio2_wt_pct!, y: r.mg_number!, depth: depthOf(r) }));

    // Plot 2: Eu/Eu* vs SiO₂
    const p2 = rows
        .filter((r) => r.sio2_wt_pct != null && r.eu_anomaly != null)
        .map((r) => ({ x: r.sio2_wt_pct!, y: r.eu_anomaly!, depth: depthOf(r) }));

    // Plot 3: CIA vs Depth (depth on y, so depth-down is natural)
    const p3 = rows
        .filter((r) => r.cia != null)
        .map((r) => ({ x: r.cia!, y: depthOf(r) }));

    // Plot 4: (La/Yb)_N vs Depth. Derived from REE blob.
    const p4 = rows
        .map((r) => {
            const ree = reeFor(r);
            if (!ree) return null;
            const la = ree.La_N ?? ree.la_n ?? null;
            const yb = ree.Yb_N ?? ree.yb_n ?? null;
            if (la == null || yb == null || yb === 0) return null;
            return { x: la / yb, y: depthOf(r) };
        })
        .filter((p): p is { x: number; y: number } => p !== null);

    return [
        {
            title: 'Mg# vs SiO₂',
            subtitle: 'Igneous differentiation',
            data: p1,
            trace: scatterTrace(p1, { hoverDepth: true }),
            layout: layoutXY('SiO₂ (wt%)', 'Mg#'),
        },
        {
            title: 'Eu/Eu* vs SiO₂',
            subtitle: 'Plagioclase signature (1.0 = no anomaly)',
            data: p2,
            trace: scatterTrace(p2, { hoverDepth: true, refLine: { value: 1.0, axis: 'y' } }),
            layout: layoutXY('SiO₂ (wt%)', 'Eu/Eu*', { yRefLine: 1.0 }),
        },
        {
            title: 'CIA vs Depth',
            subtitle: 'Chemical Index of Alteration',
            data: p3,
            trace: scatterTrace(p3),
            layout: layoutXY('CIA', 'Depth (m)', { yReversed: true }),
        },
        {
            title: '(La/Yb)_N vs Depth',
            subtitle: 'Light vs heavy REE fractionation',
            data: p4,
            trace: scatterTrace(p4),
            layout: layoutXY('(La/Yb)_N', 'Depth (m)', { yReversed: true }),
        },
    ];
}

function scatterTrace(
    pts: { x: number; y: number; depth?: number }[],
    opts: { hoverDepth?: boolean; refLine?: { value: number; axis: 'x' | 'y' } } = {},
) {
    return {
        type: 'scatter',
        mode: 'markers',
        x: pts.map((p) => p.x),
        y: pts.map((p) => p.y),
        marker: {
            size: 7,
            color: '#60a5fa',
            line: { color: '#1e40af', width: 0.5 },
        },
        text: opts.hoverDepth
            ? pts.map((p) => `Depth: ${p.depth?.toFixed(1) ?? '—'} m`)
            : undefined,
        hovertemplate: opts.hoverDepth
            ? '%{text}<br>x=%{x:.2f}<br>y=%{y:.2f}<extra></extra>'
            : 'x=%{x:.2f}<br>y=%{y:.2f}<extra></extra>',
    };
}

function layoutXY(xTitle: string, yTitle: string, opts: { yReversed?: boolean; yRefLine?: number } = {}) {
    const baseLayout: Record<string, unknown> = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#94a3b8', size: 11 },
        margin: { l: 52, r: 12, t: 6, b: 40 },
        xaxis: {
            title: { text: xTitle, font: { color: '#cbd5e1' } },
            gridcolor: 'rgba(148,163,184,0.18)',
            zerolinecolor: 'rgba(148,163,184,0.3)',
            color: '#94a3b8',
        },
        yaxis: {
            title: { text: yTitle, font: { color: '#cbd5e1' } },
            gridcolor: 'rgba(148,163,184,0.18)',
            zerolinecolor: 'rgba(148,163,184,0.3)',
            color: '#94a3b8',
            autorange: opts.yReversed ? 'reversed' : true,
        },
        showlegend: false,
    };

    if (opts.yRefLine != null) {
        baseLayout.shapes = [
            {
                type: 'line',
                xref: 'paper',
                yref: 'y',
                x0: 0,
                x1: 1,
                y0: opts.yRefLine,
                y1: opts.yRefLine,
                line: { color: 'rgba(239,68,68,0.5)', width: 1, dash: 'dash' },
            },
        ];
    }

    return baseLayout;
}
