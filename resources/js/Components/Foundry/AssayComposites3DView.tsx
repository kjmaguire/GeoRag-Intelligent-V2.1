import { useMemo, useState } from 'react';
import GeoPlot from '@/Components/GeoPlot';

interface Composite {
    collar_id: string;
    element: string;
    from_depth: number;
    to_depth: number;
    weighted_avg: number;
    unit: string;
    cutoff_grade: number | null;
    sample_count: number | null;
}

interface CollarPoint {
    collar_id: string;
    hole_id: string;
    hole_id_canonical: string;
    easting: number | null;
    northing: number | null;
    total_depth: number | null;
}

interface AssayElement {
    element: string;
    count: number;
}

/**
 * AssayComposites3DView — vertical hole sticks with each band coloured by
 * the composited weighted-average grade for the selected element. Built
 * on the same UTM-centred + depth-down convention as Borehole3DView so
 * the camera defaults feel familiar across sub-views. The element picker
 * lives in the card body (not the global toolbar) because it only makes
 * sense within this sub-view.
 */
export default function AssayComposites3DView({
    collars,
    composites,
    elements,
    height = 560,
}: {
    collars: CollarPoint[];
    composites: Composite[];
    elements: AssayElement[];
    height?: number;
}) {
    // Default to the most-common element (already sorted desc in the
    // controller). Falls back to '' which yields an empty render.
    const [selected, setSelected] = useState<string>(elements[0]?.element ?? '');

    const filtered = useMemo(
        () => composites.filter((c) => c.element === selected),
        [composites, selected],
    );

    const { data, layout, gradeRange } = useMemo(() => {
        const valid = collars.filter((c) => c.easting !== null && c.northing !== null);
        if (valid.length === 0 || filtered.length === 0) {
            return { data: [] as Record<string, unknown>[], layout: {} as Record<string, unknown>, gradeRange: [0, 0] as [number, number] };
        }

        const meanE = valid.reduce((s, c) => s + (c.easting ?? 0), 0) / valid.length;
        const meanN = valid.reduce((s, c) => s + (c.northing ?? 0), 0) / valid.length;

        const grades = filtered.map((c) => c.weighted_avg);
        const gMin = Math.min(...grades);
        const gMax = Math.max(...grades);

        // 5-stop viridis-ish palette — cool → warm as grade climbs.
        const palette = ['#22085c', '#3b4a98', '#268d8a', '#7bc56a', '#f5e95b'];
        const colorFor = (v: number): string => {
            if (gMax <= gMin) return palette[Math.floor(palette.length / 2)];
            const t = (v - gMin) / (gMax - gMin);
            const idx = Math.min(palette.length - 1, Math.max(0, Math.floor(t * (palette.length - 1))));
            return palette[idx];
        };

        // Index composites by collar for fast lookup.
        const byCollar = new Map<string, Composite[]>();
        for (const c of filtered) {
            const arr = byCollar.get(c.collar_id) ?? [];
            arr.push(c);
            byCollar.set(c.collar_id, arr);
        }

        const traces: Record<string, unknown>[] = [];
        const allDepths: number[] = [];

        valid.forEach((collar) => {
            const x0 = (collar.easting as number) - meanE;
            const y0 = (collar.northing as number) - meanN;
            const td = collar.total_depth ?? 0;

            // Faint baseline trace for the full hole so empty holes still
            // show as ghost sticks alongside hit holes.
            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: [x0, x0],
                y: [y0, y0],
                z: [0, -td],
                line: { color: 'rgba(155,169,184,0.18)', width: 2 },
                hoverinfo: 'name',
                showlegend: false,
                name: collar.hole_id_canonical || collar.hole_id,
            });

            const bands = byCollar.get(collar.collar_id) ?? [];
            if (bands.length === 0) return;

            const xs: number[] = [];
            const ys: number[] = [];
            const zs: number[] = [];
            const colors: string[] = [];
            const text: string[] = [];

            for (const b of bands) {
                xs.push(x0, x0);
                ys.push(y0, y0);
                zs.push(-b.from_depth, -b.to_depth);
                const col = colorFor(b.weighted_avg);
                colors.push(col, col);
                const desc = `${collar.hole_id_canonical || collar.hole_id} · ${b.from_depth.toFixed(1)}–${b.to_depth.toFixed(1)} m · ${b.weighted_avg.toFixed(3)} ${b.unit}${b.sample_count !== null ? ` · ${b.sample_count} samples` : ''}`;
                text.push(desc, desc);
                allDepths.push(b.to_depth);
            }

            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: xs,
                y: ys,
                z: zs,
                line: { color: colors, width: 7 },
                text,
                hoverinfo: 'text',
                showlegend: false,
                name: collar.hole_id_canonical || collar.hole_id,
            });
        });

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
            showlegend: false,
            hovermode: 'closest',
        };

        return { data: traces, layout: layoutObj, gradeRange: [gMin, gMax] as [number, number] };
    }, [collars, filtered]);

    if (elements.length === 0) {
        return (
            <div className="text-[11px] font-mono p-6 text-center" style={{ color: 'var(--fg-3)' }}>
                0 rows in gold.assay_composites for this project — composite pipeline hasn't run yet.
            </div>
        );
    }

    return (
        <div className="flex flex-col h-full min-h-0">
            <div className="flex items-center gap-3 mb-2 shrink-0 flex-wrap">
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Element</span>
                <select
                    value={selected}
                    onChange={(e) => setSelected(e.target.value)}
                    className="text-[11px] font-mono px-2 py-1 rounded border"
                    style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
                >
                    {elements.map((el) => (
                        <option key={el.element} value={el.element}>{el.element} ({el.count})</option>
                    ))}
                </select>
                {filtered.length > 0 && (
                    <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>
                        {filtered.length} composites · grade range {gradeRange[0].toFixed(3)}–{gradeRange[1].toFixed(3)} {filtered[0]?.unit ?? ''}
                    </span>
                )}
            </div>
            {data.length === 0 ? (
                <div className="text-[11px] font-mono p-6 text-center" style={{ color: 'var(--fg-3)' }}>
                    No composites for the selected element.
                </div>
            ) : (
                <div className="flex-1 min-h-0" style={{ height }}>
                    <GeoPlot data={data} layout={layout} />
                </div>
            )}
        </div>
    );
}
