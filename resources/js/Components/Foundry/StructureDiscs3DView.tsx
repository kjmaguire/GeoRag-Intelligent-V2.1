import { useMemo, useState } from 'react';
import GeoPlot from '@/Components/GeoPlot';

interface StructureVisual {
    collar_id: string;
    strike_deg: number;
    dip_deg: number;
    measurement_kind: string;
    depth_m: number | null;
    pole_trend_deg: number;
    pole_plunge_deg: number;
    display_color: string | null;
    display_symbol: string | null;
    confidence: string | null;
}

interface CollarPoint {
    collar_id: string;
    hole_id: string;
    hole_id_canonical: string;
    easting: number | null;
    northing: number | null;
    total_depth: number | null;
}

const KIND_FALLBACK_COLORS: Record<string, string> = {
    bedding: '#3b82f6',
    foliation: '#a855f7',
    fault: '#ef4444',
    shear: '#f97316',
    joint: '#14b8a6',
    fracture: '#eab308',
    vein: '#22c55e',
    lineation: '#ec4899',
};

/**
 * StructureDiscs3DView — collar sticks plus an oriented disc primitive at
 * each structure measurement's depth. Disc orientation derives from the
 * measurement's strike + dip: we draw a small circle in the plane
 * perpendicular to the pole. Different from Stereosphere — that one
 * abstracts measurements onto a unit sphere; this one anchors them in
 * real-world space so spatial clustering shows up.
 */
export default function StructureDiscs3DView({
    collars,
    structures,
    height = 560,
}: {
    collars: CollarPoint[];
    structures: StructureVisual[];
    height?: number;
}) {
    const kindOptions = useMemo(() => {
        const counts = new Map<string, number>();
        for (const s of structures) counts.set(s.measurement_kind, (counts.get(s.measurement_kind) ?? 0) + 1);
        return Array.from(counts.entries())
            .sort((a, b) => b[1] - a[1])
            .map(([kind, count]) => ({ kind, count }));
    }, [structures]);

    const [visibleKinds, setVisibleKinds] = useState<Record<string, boolean>>(() => {
        const init: Record<string, boolean> = {};
        for (const { kind } of kindOptions) init[kind] = true;
        return init;
    });

    const filtered = useMemo(
        () => structures.filter((s) => visibleKinds[s.measurement_kind] !== false && s.depth_m !== null),
        [structures, visibleKinds],
    );

    const { data, layout } = useMemo(() => {
        const valid = collars.filter((c) => c.easting !== null && c.northing !== null);
        if (valid.length === 0) {
            return { data: [] as Record<string, unknown>[], layout: {} as Record<string, unknown> };
        }

        const meanE = valid.reduce((s, c) => s + (c.easting ?? 0), 0) / valid.length;
        const meanN = valid.reduce((s, c) => s + (c.northing ?? 0), 0) / valid.length;

        const collarById = new Map(valid.map((c) => [c.collar_id, c]));

        // Disc radius — calibrated so discs read at the campaign scale
        // without dominating the scene. Tuned for ~100-1000m UTM extents.
        const DISC_R = 12;
        const DISC_PTS = 24;

        const traces: Record<string, unknown>[] = [];
        const allDepths: number[] = [];

        // Faint collar trace per hole for spatial context.
        for (const c of valid) {
            const x0 = (c.easting as number) - meanE;
            const y0 = (c.northing as number) - meanN;
            const td = c.total_depth ?? 0;
            allDepths.push(td);
            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: [x0, x0],
                y: [y0, y0],
                z: [0, -td],
                line: { color: 'rgba(155,169,184,0.18)', width: 1.5 },
                hoverinfo: 'name',
                showlegend: false,
                name: c.hole_id_canonical || c.hole_id,
            });
        }

        // Group structures by kind so each gets a single legend row + colour.
        const byKind = new Map<string, StructureVisual[]>();
        for (const s of filtered) {
            const arr = byKind.get(s.measurement_kind) ?? [];
            arr.push(s);
            byKind.set(s.measurement_kind, arr);
        }

        let legendIdx = 0;
        for (const [kind, rows] of byKind.entries()) {
            const color = rows[0]?.display_color ?? KIND_FALLBACK_COLORS[kind] ?? '#94a3b8';
            // Concatenated disc polylines with null separators between discs.
            const xAll: (number | null)[] = [];
            const yAll: (number | null)[] = [];
            const zAll: (number | null)[] = [];
            const poleXs: number[] = [];
            const poleYs: number[] = [];
            const poleZs: number[] = [];
            const poleText: string[] = [];

            for (const s of rows) {
                const collar = collarById.get(s.collar_id);
                if (!collar || s.depth_m === null) continue;
                const x0 = (collar.easting as number) - meanE;
                const y0 = (collar.northing as number) - meanN;
                const z0 = -s.depth_m;
                allDepths.push(s.depth_m);

                // Build a disc in the plane perpendicular to the pole.
                // Pole vector (unit) from (trend, plunge):
                const trRad = (s.pole_trend_deg * Math.PI) / 180;
                const plRad = (s.pole_plunge_deg * Math.PI) / 180;
                const px = Math.cos(plRad) * Math.sin(trRad);
                const py = Math.cos(plRad) * Math.cos(trRad);
                const pz = -Math.sin(plRad);

                // Two basis vectors orthogonal to the pole (used to sweep
                // the disc circumference). Choose `up` ≠ pole then cross.
                const up: [number, number, number] = Math.abs(pz) < 0.95 ? [0, 0, 1] : [0, 1, 0];
                // u = pole × up, normalised
                const ux = py * up[2] - pz * up[1];
                const uy = pz * up[0] - px * up[2];
                const uz = px * up[1] - py * up[0];
                const uLen = Math.hypot(ux, uy, uz) || 1;
                const ub: [number, number, number] = [ux / uLen, uy / uLen, uz / uLen];
                // v = pole × u
                const vb: [number, number, number] = [
                    py * ub[2] - pz * ub[1],
                    pz * ub[0] - px * ub[2],
                    px * ub[1] - py * ub[0],
                ];

                for (let i = 0; i <= DISC_PTS; i++) {
                    const t = (i / DISC_PTS) * 2 * Math.PI;
                    const cT = Math.cos(t) * DISC_R;
                    const sT = Math.sin(t) * DISC_R;
                    xAll.push(x0 + ub[0] * cT + vb[0] * sT);
                    yAll.push(y0 + ub[1] * cT + vb[1] * sT);
                    zAll.push(z0 + ub[2] * cT + vb[2] * sT);
                }
                xAll.push(null); yAll.push(null); zAll.push(null);

                poleXs.push(x0);
                poleYs.push(y0);
                poleZs.push(z0);
                const holeId = collar.hole_id_canonical || collar.hole_id;
                poleText.push(`${holeId} · ${kind} · strike ${s.strike_deg.toFixed(0)}° · dip ${s.dip_deg.toFixed(0)}° @ ${s.depth_m.toFixed(1)} m${s.confidence ? ` · ${s.confidence}` : ''}`);
            }

            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: xAll,
                y: yAll,
                z: zAll,
                line: { color, width: 3 },
                hoverinfo: 'skip',
                showlegend: legendIdx === 0,
                name: `${kind} (${rows.length})`,
                legendgroup: kind,
            });
            traces.push({
                type: 'scatter3d',
                mode: 'markers',
                x: poleXs,
                y: poleYs,
                z: poleZs,
                marker: { size: 3, color, symbol: 'circle' },
                text: poleText,
                hoverinfo: 'text',
                showlegend: false,
                name: `${kind} centre`,
                legendgroup: kind,
            });
            legendIdx += 1;
        }

        const maxDepth = Math.max(...allDepths, 100);

        const layoutObj: Record<string, unknown> = {
            scene: {
                xaxis: {
                    title: { text: 'Easting (m)', font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                yaxis: {
                    title: { text: 'Northing (m)', font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                zaxis: {
                    title: { text: 'Depth (m)', font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                    range: [-maxDepth * 1.1, 10],
                },
                bgcolor: '#0a0e14',
                aspectmode: 'manual',
                aspectratio: { x: 1, y: 1, z: 0.6 },
                camera: { eye: { x: 1.6, y: 1.6, z: 0.8 }, up: { x: 0, y: 0, z: 1 } },
            },
            paper_bgcolor: '#0a0e14',
            plot_bgcolor: '#0a0e14',
            margin: { l: 0, r: 0, t: 0, b: 0 },
            showlegend: true,
            legend: { font: { color: '#cbd5e1', size: 10 }, bgcolor: 'rgba(15,23,42,0.6)' },
            hovermode: 'closest',
        };

        return { data: traces, layout: layoutObj };
    }, [collars, filtered]);

    if (kindOptions.length === 0) {
        return (
            <div className="text-[11px] font-mono p-6 text-center" style={{ color: 'var(--fg-3)' }}>
                0 rows in gold.structure_measurements_visual for this project — no enriched stereonet-ready measurements yet.
            </div>
        );
    }

    return (
        <div className="flex flex-col h-full min-h-0">
            <div className="flex items-center gap-3 mb-2 shrink-0 flex-wrap">
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Kinds</span>
                {kindOptions.map(({ kind, count }) => (
                    <label key={kind} className="flex items-center gap-1.5 text-[11px] font-mono cursor-pointer">
                        <input
                            type="checkbox"
                            checked={visibleKinds[kind] !== false}
                            onChange={(e) => setVisibleKinds((p) => ({ ...p, [kind]: e.target.checked }))}
                        />
                        <span style={{ color: 'var(--fg-1)' }}>{kind}</span>
                        <span style={{ color: 'var(--fg-3)' }}>({count})</span>
                    </label>
                ))}
            </div>
            <div className="flex-1 min-h-0" style={{ height }}>
                <GeoPlot data={data} layout={layout} />
            </div>
        </div>
    );
}
