import { useMemo, useState } from 'react';
import GeoPlot from '@/Components/GeoPlot';

interface CommoditySample {
    collar_id: string;
    from_depth: number;
    to_depth: number;
    sample_type: string;
    grades: Record<string, number>;
}

interface CommodityKey {
    key: string;
    count: number;
}

interface CollarPoint {
    collar_id: string;
    hole_id: string;
    hole_id_canonical: string;
    easting: number | null;
    northing: number | null;
    total_depth: number | null;
}

/**
 * CommoditySamples3DView — vertical hole sticks coloured by per-sample
 * commodity grade pulled from silver.samples.commodity_assays. For
 * Cameco Shirley Basin this is the only place uranium (U3O8_pct_e)
 * grade surfaces at hole+depth resolution; gold.assay_composites is
 * REE/geochem-heavy and doesn't carry uranium.
 *
 * Distinct from Assay Grade which renders gold.assay_composites
 * (composited assay intervals for non-commodity elements). This view
 * renders each commodity sample row (silver.samples) coloured by grade.
 */
export default function CommoditySamples3DView({
    collars,
    samples,
    commodityKeys,
    height = 560,
}: {
    collars: CollarPoint[];
    samples: CommoditySample[];
    commodityKeys: CommodityKey[];
    height?: number;
}) {
    const [selected, setSelected] = useState<string>(commodityKeys[0]?.key ?? '');

    const filtered = useMemo(() => {
        if (!selected) return [] as CommoditySample[];
        return samples.filter((s) => Object.prototype.hasOwnProperty.call(s.grades, selected));
    }, [samples, selected]);

    const { data, layout, gradeRange, unit } = useMemo(() => {
        const valid = collars.filter((c) => c.easting !== null && c.northing !== null);
        if (valid.length === 0 || filtered.length === 0) {
            return {
                data: [] as Record<string, unknown>[],
                layout: {} as Record<string, unknown>,
                gradeRange: [0, 0] as [number, number],
                unit: '',
            };
        }

        const meanE = valid.reduce((s, c) => s + (c.easting ?? 0), 0) / valid.length;
        const meanN = valid.reduce((s, c) => s + (c.northing ?? 0), 0) / valid.length;

        const grades = filtered.map((s) => s.grades[selected]);
        const gMin = Math.min(...grades);
        const gMax = Math.max(...grades);

        // Heat palette — ore-grade reds at the top.
        const palette = ['#1e3a8a', '#0ea5e9', '#22c55e', '#facc15', '#f97316', '#dc2626'];
        const colorFor = (v: number): string => {
            if (gMax <= gMin) return palette[Math.floor(palette.length / 2)];
            const t = (v - gMin) / (gMax - gMin);
            const idx = Math.min(palette.length - 1, Math.max(0, Math.floor(t * (palette.length - 1))));
            return palette[idx];
        };

        // Group filtered samples by collar_id for fast lookup.
        const byCollar = new Map<string, CommoditySample[]>();
        for (const s of filtered) {
            const arr = byCollar.get(s.collar_id) ?? [];
            arr.push(s);
            byCollar.set(s.collar_id, arr);
        }

        const traces: Record<string, unknown>[] = [];
        const allDepths: number[] = [];

        // Guess a unit hint from the key name. Most U3O8_pct_e values are
        // fractions of a percent so we just label as the raw key for now.
        const unitLabel = selected.endsWith('_pct') || selected.endsWith('_pct_e') ? '%'
            : selected.endsWith('_ppm') ? 'ppm' : '';

        for (const collar of valid) {
            const x0 = (collar.easting as number) - meanE;
            const y0 = (collar.northing as number) - meanN;
            const td = collar.total_depth ?? 0;

            // Ghost trace for every hole.
            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: [x0, x0],
                y: [y0, y0],
                z: [0, -td],
                line: { color: 'rgba(155,169,184,0.16)', width: 1.5 },
                hoverinfo: 'name',
                showlegend: false,
                name: collar.hole_id_canonical || collar.hole_id,
            });

            const holeSamples = byCollar.get(collar.collar_id) ?? [];
            if (holeSamples.length === 0) continue;

            const xs: number[] = [];
            const ys: number[] = [];
            const zs: number[] = [];
            const colors: string[] = [];
            const text: string[] = [];
            for (const s of holeSamples) {
                const v = s.grades[selected];
                xs.push(x0, x0);
                ys.push(y0, y0);
                zs.push(-s.from_depth, -s.to_depth);
                const c = colorFor(v);
                colors.push(c, c);
                const desc = `${collar.hole_id_canonical || collar.hole_id} · ${s.from_depth.toFixed(1)}–${s.to_depth.toFixed(1)} m · ${selected} = ${v.toFixed(4)}${unitLabel ? ' ' + unitLabel : ''}`;
                text.push(desc, desc);
                allDepths.push(s.to_depth);
            }

            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: xs,
                y: ys,
                z: zs,
                line: { color: colors, width: 8 },
                text,
                hoverinfo: 'text',
                showlegend: false,
                name: collar.hole_id_canonical || collar.hole_id,
            });
        }

        const maxDepth = Math.max(...allDepths, 100);
        const layoutObj: Record<string, unknown> = {
            scene: {
                xaxis: { title: { text: 'Easting (m)', font: { color: '#9ba9b8', size: 10 } }, color: '#9ba9b8', gridcolor: 'rgba(155,169,184,0.18)', backgroundcolor: '#0a0e14', showbackground: true },
                yaxis: { title: { text: 'Northing (m)', font: { color: '#9ba9b8', size: 10 } }, color: '#9ba9b8', gridcolor: 'rgba(155,169,184,0.18)', backgroundcolor: '#0a0e14', showbackground: true },
                zaxis: { title: { text: 'Depth (m)', font: { color: '#9ba9b8', size: 10 } }, color: '#9ba9b8', gridcolor: 'rgba(155,169,184,0.18)', backgroundcolor: '#0a0e14', showbackground: true, range: [-maxDepth * 1.1, 10] },
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

        return { data: traces, layout: layoutObj, gradeRange: [gMin, gMax] as [number, number], unit: unitLabel };
    }, [collars, filtered, selected]);

    if (commodityKeys.length === 0) {
        return (
            <div className="text-[11px] font-mono p-6 text-center" style={{ color: 'var(--fg-3)' }}>
                0 commodity samples in silver.samples for this project.
            </div>
        );
    }

    return (
        <div className="flex flex-col h-full min-h-0">
            <div className="flex items-center gap-3 mb-2 shrink-0 flex-wrap">
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Commodity</span>
                <select
                    value={selected}
                    onChange={(e) => setSelected(e.target.value)}
                    className="text-[11px] font-mono px-2 py-1 rounded border"
                    style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
                >
                    {commodityKeys.map((k) => (
                        <option key={k.key} value={k.key}>{k.key} ({k.count})</option>
                    ))}
                </select>
                {filtered.length > 0 && (
                    <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>
                        {filtered.length} samples · range {gradeRange[0].toFixed(4)}–{gradeRange[1].toFixed(4)}{unit ? ' ' + unit : ''}
                    </span>
                )}
            </div>
            {data.length === 0 ? (
                <div className="text-[11px] font-mono p-6 text-center" style={{ color: 'var(--fg-3)' }}>
                    No samples carry a value for {selected}.
                </div>
            ) : (
                <div className="flex-1 min-h-0" style={{ height }}>
                    <GeoPlot data={data} layout={layout} />
                </div>
            )}
        </div>
    );
}
