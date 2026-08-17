import { useMemo } from 'react';
import GeoPlot from '@/Components/GeoPlot';

interface IntervalBand {
    from: number;
    to: number;
    code: string;
    color: string;
}

interface HoleIntervalRow {
    hole_id: string;
    total_depth: number | null;
    easting: number | null;
    northing: number | null;
    lat: number | null;
    lng: number | null;
    bands: IntervalBand[];
}

/**
 * Borehole3DView — 3D borehole viewer built on Plotly Scatter3d.
 *
 * Uses the project's existing GeoPlot wrapper rather than
 * `react-plotly.js/factory`. That factory crashes under rolldown's CJS
 * interop with "(0, M.default) is not a function" — see GeoPlot.tsx
 * for the documented workaround. GeoPlot calls Plotly.react directly
 * and avoids the broken default-export path.
 *
 * Each hole becomes a vertical line in 3D space:
 *   x = easting offset from project centroid (m)
 *   y = northing offset from project centroid (m)
 *   z = depth from surface (negative so surface is z=0, holes hang below)
 *
 * Lithology bands paint each segment via line.color array. ORE bands
 * carry the bright green from the LOGS palette so the visual identity
 * is consistent.
 */
export function Borehole3DView({
    holes,
    height = 560,
}: {
    holes: HoleIntervalRow[];
    height?: number;
}) {
    const { data, layout } = useMemo(() => {
        const valid = holes.filter((h) => h.easting !== null && h.northing !== null && h.total_depth !== null && h.bands.length > 0);
        if (valid.length === 0) {
            return { data: [] as Record<string, unknown>[], layout: {} as Record<string, unknown> };
        }

        // Project centroid in UTM metres so coords render at human scale.
        const meanE = valid.reduce((s, h) => s + (h.easting ?? 0), 0) / valid.length;
        const meanN = valid.reduce((s, h) => s + (h.northing ?? 0), 0) / valid.length;

        const traces: Record<string, unknown>[] = [];

        valid.forEach((h) => {
            const x0 = (h.easting as number) - meanE;
            const y0 = (h.northing as number) - meanN;

            const xs: number[] = [];
            const ys: number[] = [];
            const zs: number[] = [];
            const colors: string[] = [];
            const text: string[] = [];

            h.bands.forEach((b) => {
                xs.push(x0, x0);
                ys.push(y0, y0);
                zs.push(-b.from, -b.to);
                colors.push(b.color, b.color);
                const isOre = b.code.endsWith('-ORE');
                const desc = `${h.hole_id} · ${b.from.toFixed(1)}–${b.to.toFixed(1)} m · ${b.code.replace('DERIVED-', '')}${isOre ? ' (U)' : ''}`;
                text.push(desc, desc);
            });

            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: xs,
                y: ys,
                z: zs,
                line: { color: colors, width: 6 },
                text,
                hoverinfo: 'text',
                showlegend: false,
                name: h.hole_id,
            });

            const hasOre = h.bands.some((b) => b.code.endsWith('-ORE'));
            traces.push({
                type: 'scatter3d',
                mode: 'markers+text',
                x: [x0],
                y: [y0],
                z: [0],
                marker: {
                    size: 4,
                    color: hasOre ? '#8fe28b' : '#7accee',
                    line: { color: '#0a0e14', width: 1 },
                },
                text: [h.hole_id],
                textposition: 'top center',
                textfont: { color: '#e8edf3', size: 9, family: 'monospace' },
                hoverinfo: 'name',
                showlegend: false,
                name: h.hole_id,
            });
        });

        const allDepths = valid.flatMap((h) => h.bands.map((b) => b.to));
        const maxDepth = Math.max(...allDepths, 100);

        const layout: Record<string, unknown> = {
            scene: {
                xaxis: {
                    title: { text: 'Easting (m)', font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    zerolinecolor: 'rgba(155,169,184,0.32)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                yaxis: {
                    title: { text: 'Northing (m)', font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    zerolinecolor: 'rgba(155,169,184,0.32)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                zaxis: {
                    title: { text: 'Depth (m, surface = 0)', font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    zerolinecolor: 'rgba(155,169,184,0.32)',
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

        return { data: traces, layout };
    }, [holes]);

    if (data.length === 0) {
        return (
            <div className="text-[11px] font-mono p-6 text-center" style={{ color: 'var(--fg-3)' }}>
                No 3D interval data — derive_intervals hasn't run for this project.
            </div>
        );
    }

    return (
        <div style={{ width: '100%', height }}>
            <GeoPlot data={data} layout={layout} />
        </div>
    );
}
