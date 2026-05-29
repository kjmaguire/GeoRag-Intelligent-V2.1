import { useEffect, useMemo, useRef } from 'react';
import Plotly from 'plotly.js-dist-min';

/**
 * 2026-05-26 — DO NOT re-import `react-plotly.js/factory`. Rolldown's
 * CJS-to-ESM interop wraps it such that calling `createPlotlyComponent`
 * crashes at runtime with `(0, o.default) is not a function`. See
 * GeoPlot.tsx's docblock for the full diagnosis. The fix here mirrors
 * GeoPlot: imperative `PlotlyAPI.react(div, traces, layout, config)`
 * with a `useRef` host and `PlotlyAPI.purge` on unmount.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const PlotlyAPI: any = (Plotly as any).default ?? Plotly;

/**
 * DrillTrace3D — Plotly 3D scatter of drillhole collars and (optionally)
 * downhole curve traces, color-coded intervals, and structural pole markers.
 *
 * ADR-0007 PR-4: added `trace_points`, `intervals`, and `structures`
 * overlays. All three are additive — a payload with only `collars[]`
 * still renders the original collar dots + straight vertical tubes.
 */

export interface TracePoint {
    x: number;          // longitude / easting (matches collar.longitude)
    y: number;          // latitude / northing (matches collar.latitude)
    z: number;          // elevation (m, RL)
    depth_m: number;    // downhole depth measured from collar (0 at top)
}

export interface CollarPoint {
    longitude: number;
    latitude: number;
    elevation: number;
    total_depth: number;
    hole_id: string;
    hole_type: string;
    status: string;
    collar_id?: string;
    azimuth?: number;
    dip?: number;
    /**
     * Survey-aware trace from collar to toe. When present and length >= 2,
     * the per-hole tube uses these points (allows deviated holes); when
     * absent we synthesize a straight vertical 2-point trace from
     * (elevation) → (elevation - total_depth).
     */
    trace_points?: TracePoint[];
}

export interface IntervalPoint {
    collar_id: string;
    depth_from: number;
    depth_to: number;
    interval_kind: string;       // 'assay'|'lithology'|'alteration'|'structure'
    color_hint: string;          // hex e.g. '#a83232' — from gold.drillhole_intervals_visual
    label: string;
}

export interface StructurePoint {
    collar_id: string;
    depth: number;
    structure_type: string;
    strike_deg: number | null;
    dip_deg: number | null;
    source_row_id: string;
}

interface DrillTrace3DProps {
    collars?: CollarPoint[];
    intervals?: IntervalPoint[];
    structures?: StructurePoint[];
}

const STATUS_COLORS: Record<string, string> = {
    Completed: '#22c55e',
    Active: '#eab308',
    'In Progress': '#eab308',
    Abandoned: '#ef4444',
};

const STRUCTURE_COLORS: Record<string, string> = {
    foliation: '#3b82f6',  // blue
    joint: '#ef4444',      // red
    fault: '#f97316',      // orange
    vein: '#22c55e',       // green
    bedding: '#a855f7',    // purple
    shear: '#ec4899',      // pink
    fracture: '#facc15',   // yellow
    lineation: '#06b6d4',  // cyan
};

const STRUCTURE_DEFAULT_COLOR = '#9ca3af';

/**
 * Build a (synthetic-if-needed) trace_points array for a collar:
 *   - If the payload supplies trace_points with >= 2 entries, use them as-is.
 *   - Otherwise generate a straight 2-point vertical from the collar
 *     elevation down by total_depth (legacy behavior).
 */
function effectiveTrace(c: CollarPoint): TracePoint[] {
    if (Array.isArray(c.trace_points) && c.trace_points.length >= 2) {
        return c.trace_points;
    }
    const top = c.elevation || 0;
    const td = c.total_depth || 0;
    return [
        { x: c.longitude, y: c.latitude, z: top, depth_m: 0 },
        { x: c.longitude, y: c.latitude, z: top - td, depth_m: td },
    ];
}

/**
 * Linear-interpolate an (x,y,z) point along a sorted-by-depth trace
 * for an arbitrary downhole depth. Clamps to endpoints when the depth
 * falls outside the trace's depth range.
 */
function interpolateAtDepth(
    trace: TracePoint[],
    depth: number,
): { x: number; y: number; z: number } | null {
    if (!trace.length) return null;
    if (depth <= trace[0].depth_m) {
        return { x: trace[0].x, y: trace[0].y, z: trace[0].z };
    }
    const last = trace[trace.length - 1];
    if (depth >= last.depth_m) {
        return { x: last.x, y: last.y, z: last.z };
    }
    for (let i = 0; i < trace.length - 1; i++) {
        const a = trace[i];
        const b = trace[i + 1];
        if (depth >= a.depth_m && depth <= b.depth_m) {
            const span = b.depth_m - a.depth_m || 1;
            const t = (depth - a.depth_m) / span;
            return {
                x: a.x + (b.x - a.x) * t,
                y: a.y + (b.y - a.y) * t,
                z: a.z + (b.z - a.z) * t,
            };
        }
    }
    return { x: last.x, y: last.y, z: last.z };
}

export default function DrillTrace3D({
    collars = [],
    intervals = [],
    structures = [],
}: DrillTrace3DProps) {
    const { traces, layout } = useMemo(() => {
        if (collars.length === 0) return { traces: [] as Record<string, unknown>[], layout: {} };

        const byStatus: Record<string, CollarPoint[]> = {};
        collars.forEach((c) => {
            const status = c.status || 'Unknown';
            if (!byStatus[status]) byStatus[status] = [];
            byStatus[status].push(c);
        });

        const traces: Record<string, unknown>[] = [];

        // ── (1) Collar dots + bare trace tubes, grouped by status ───────────
        Object.entries(byStatus).forEach(([status, holes]) => {
            const color = STATUS_COLORS[status] || '#6b7280';

            traces.push({
                type: 'scatter3d',
                mode: 'markers+text',
                name: status,
                x: holes.map((h) => h.longitude),
                y: holes.map((h) => h.latitude),
                z: holes.map((h) => h.elevation || 0),
                text: holes.map((h) => h.hole_id),
                textposition: 'top center',
                textfont: { size: 9, color: '#d1d5db', family: 'monospace' },
                marker: {
                    size: 5,
                    color,
                    symbol: 'diamond',
                    line: { width: 1, color: '#ffffff' },
                },
                hovertemplate:
                    '<b>%{text}</b><br>' +
                    'Lon: %{x:.4f}<br>Lat: %{y:.4f}<br>' +
                    'Elev: %{z:.0f} m<extra></extra>',
            });

            holes.forEach((h) => {
                const trace = effectiveTrace(h);
                traces.push({
                    type: 'scatter3d',
                    mode: 'lines',
                    showlegend: false,
                    x: trace.map((p) => p.x),
                    y: trace.map((p) => p.y),
                    z: trace.map((p) => p.z),
                    line: { color, width: 3 },
                    hoverinfo: 'skip',
                });
            });
        });

        // ── (2) Interval overlays, batched by color_hint ────────────────────
        // Looking up collars by collar_id (preferred) with a fallback to
        // hole_id so payloads that pre-date collar_id wiring still work.
        if (intervals.length > 0) {
            const collarLookup = new Map<string, CollarPoint>();
            collars.forEach((c) => {
                if (c.collar_id) collarLookup.set(c.collar_id, c);
                if (c.hole_id) collarLookup.set(c.hole_id, c);
            });

            // Group: color_hint → segments[]
            const byColor: Record<
                string,
                Array<{ from: { x: number; y: number; z: number }; to: { x: number; y: number; z: number }; iv: IntervalPoint }>
            > = {};
            intervals.forEach((iv) => {
                const collar = collarLookup.get(iv.collar_id);
                if (!collar) return;
                const trace = effectiveTrace(collar);
                const from = interpolateAtDepth(trace, iv.depth_from);
                const to = interpolateAtDepth(trace, iv.depth_to);
                if (!from || !to) return;
                const color = iv.color_hint || '#a3a3a3';
                if (!byColor[color]) byColor[color] = [];
                byColor[color].push({ from, to, iv });
            });

            Object.entries(byColor).forEach(([color, segments]) => {
                // Use `null` separators in x/y/z so a single scatter3d
                // trace renders many disconnected line segments — keeps
                // trace count = unique colors instead of 3300.
                const xs: (number | null)[] = [];
                const ys: (number | null)[] = [];
                const zs: (number | null)[] = [];
                const text: (string | null)[] = [];
                segments.forEach(({ from, to, iv }) => {
                    const label = `${iv.label} · ${iv.depth_from}-${iv.depth_to}m`;
                    xs.push(from.x, to.x, null);
                    ys.push(from.y, to.y, null);
                    zs.push(from.z, to.z, null);
                    text.push(label, label, null);
                });
                traces.push({
                    type: 'scatter3d',
                    mode: 'lines',
                    name: `Interval ${color}`,
                    showlegend: false,
                    x: xs,
                    y: ys,
                    z: zs,
                    text,
                    line: { color, width: 6 },
                    hovertemplate: '%{text}<extra></extra>',
                    connectgaps: false,
                });
            });
        }

        // ── (3) Structural pole markers ─────────────────────────────────────
        if (structures.length > 0) {
            const collarLookup = new Map<string, CollarPoint>();
            collars.forEach((c) => {
                if (c.collar_id) collarLookup.set(c.collar_id, c);
                if (c.hole_id) collarLookup.set(c.hole_id, c);
            });

            // Group by structure_type so the legend explains the color key.
            const byKind: Record<
                string,
                Array<{ pt: { x: number; y: number; z: number }; s: StructurePoint }>
            > = {};
            structures.forEach((s) => {
                const collar = collarLookup.get(s.collar_id);
                if (!collar) return;
                const trace = effectiveTrace(collar);
                const pt = interpolateAtDepth(trace, s.depth);
                if (!pt) return;
                const kind = s.structure_type || 'unknown';
                if (!byKind[kind]) byKind[kind] = [];
                byKind[kind].push({ pt, s });
            });

            Object.entries(byKind).forEach(([kind, items]) => {
                const color = STRUCTURE_COLORS[kind.toLowerCase()] || STRUCTURE_DEFAULT_COLOR;
                traces.push({
                    type: 'scatter3d',
                    mode: 'markers',
                    name: kind,
                    x: items.map((i) => i.pt.x),
                    y: items.map((i) => i.pt.y),
                    z: items.map((i) => i.pt.z),
                    text: items.map((i) => {
                        const strike = i.s.strike_deg ?? '—';
                        const dip = i.s.dip_deg ?? '—';
                        return `${kind} · ${i.s.depth}m · strike ${strike}/dip ${dip}`;
                    }),
                    marker: {
                        size: 4,
                        color,
                        symbol: 'diamond',
                        line: { width: 1, color: '#ffffff' },
                    },
                    hovertemplate: '%{text}<extra></extra>',
                });
            });
        }

        const layout = {
            scene: {
                xaxis: { title: { text: 'Longitude', font: { color: '#9ca3af', size: 10 } }, color: '#6b7280', gridcolor: '#1f2937', zerolinecolor: '#374151' },
                yaxis: { title: { text: 'Latitude', font: { color: '#9ca3af', size: 10 } }, color: '#6b7280', gridcolor: '#1f2937', zerolinecolor: '#374151' },
                zaxis: { title: { text: 'Elevation (m)', font: { color: '#9ca3af', size: 10 } }, color: '#6b7280', gridcolor: '#1f2937', zerolinecolor: '#374151' },
                bgcolor: '#030712',
                camera: { eye: { x: 1.5, y: 1.5, z: 0.8 } },
            },
            paper_bgcolor: '#111827',
            plot_bgcolor: '#030712',
            legend: {
                font: { color: '#d1d5db', size: 10 },
                bgcolor: 'rgba(17,24,39,0.9)',
                bordercolor: '#374151',
                borderwidth: 1,
            },
            margin: { t: 10, r: 10, b: 10, l: 10 },
        };

        return { traces, layout };
    }, [collars, intervals, structures]);

    const config = useMemo(
        () => ({
            responsive: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['toImage', 'sendDataToCloud'] as string[],
        }),
        [],
    );

    // Imperative Plotly mount/update — same pattern as GeoPlot.tsx. See the
    // factory-import warning at the top of this file for why.
    const divRef = useRef<HTMLDivElement | null>(null);

    const mergedLayout = useMemo(
        () => ({ ...(layout as Record<string, unknown>), autosize: true }),
        [layout],
    );

    useEffect(() => {
        const el = divRef.current;
        if (!el) return;
        if (typeof PlotlyAPI?.react !== 'function') {
            // eslint-disable-next-line no-console
            console.error('[DrillTrace3D] Plotly API missing .react method:', PlotlyAPI);
            return;
        }
        PlotlyAPI.react(el, traces, mergedLayout, config);
    }, [traces, mergedLayout, config]);

    useEffect(() => {
        const el = divRef.current;
        return () => {
            if (el && typeof PlotlyAPI?.purge === 'function') {
                PlotlyAPI.purge(el);
            }
        };
    }, []);

    useEffect(() => {
        const el = divRef.current;
        if (!el || typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(() => {
            if (typeof PlotlyAPI?.Plots?.resize === 'function') {
                try { PlotlyAPI.Plots.resize(el); } catch { /* ignore */ }
            }
        });
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    return (
        <div
            ref={divRef}
            className="w-full h-full"
            style={{ width: '100%', height: '100%' }}
        />
    );
}
