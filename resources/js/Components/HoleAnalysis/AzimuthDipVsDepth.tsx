import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';

interface Survey {
    depth: number;
    azimuth: number | null;
    dip: number | null;
}

interface Props {
    surveys: Survey[];
    mode: 'azimuth' | 'dip';
    /** '2d' = classic line-chart; '3d' = helix (azimuth) / slant (dip) in Plotly 3D. */
    view: '2d' | '3d';
    collarValue: number | null;  // collar orientation at depth=0
    /** For dip 3D we also want azimuth so the slant trajectory points
     *  the right way in the horizontal plane. Optional; falls back to 0°. */
    collarAzimuth?: number | null;
}

/**
 * Azimuth-vs-Depth / Dip-vs-Depth chart in either 2D or 3D.
 *
 *   Azimuth 2D  — line chart, depth on Y (reversed)
 *   Azimuth 3D  — cylindrical helix: at each station, place a marker at
 *                 (cos(az), sin(az), -depth). Shows azimuth rotation
 *                 and any rapid direction changes as a twist in the helix.
 *
 *   Dip 2D      — line chart, depth on Y (reversed)
 *   Dip 3D      — slant trajectory: station-by-station projection onto
 *                 the azimuth plane, ignoring lateral drift. Shows how
 *                 the hole's descent rate changes with depth.
 */
export default function AzimuthDipVsDepth({ surveys, mode, view, collarValue, collarAzimuth }: Props) {
    const { data2d, data3d, layout2d, layout3d, hasData } = useMemo(() => {
        // Augment with collar station at depth=0 so the curve starts
        // from the collar orientation even if the first downhole survey
        // is at non-zero depth.
        const pts: { depth: number; azimuth: number | null; dip: number | null }[] = [];
        if (mode === 'azimuth' && collarValue != null) {
            pts.push({ depth: 0, azimuth: collarValue, dip: collarAzimuth ?? null });
        } else if (mode === 'dip' && collarValue != null) {
            pts.push({ depth: 0, azimuth: collarAzimuth ?? 0, dip: collarValue });
        }
        for (const s of surveys) {
            pts.push({ depth: s.depth, azimuth: s.azimuth, dip: s.dip });
        }
        pts.sort((a, b) => a.depth - b.depth);

        // 2D track: just the scalar of interest.
        const scalarPts = pts
            .filter((p) => (mode === 'azimuth' ? p.azimuth : p.dip) != null)
            .map((p) => ({ d: p.depth, v: (mode === 'azimuth' ? p.azimuth : p.dip) as number }));

        if (scalarPts.length < 2) {
            return { data2d: null, data3d: null, layout2d: {}, layout3d: {}, hasData: false };
        }

        // ── 2D traces + layout ────────────────────────────────────────
        const trace2d = {
            type: 'scatter',
            mode: 'lines+markers',
            x: scalarPts.map((p) => p.v),
            y: scalarPts.map((p) => p.d),
            line: { color: '#22d3ee', width: 2 },
            marker: { size: 5, color: '#22d3ee' },
            hovertemplate: mode === 'azimuth'
                ? 'Azimuth: %{x:.1f}°<br>Depth: %{y:.1f} m<extra></extra>'
                : 'Dip: %{x:.1f}°<br>Depth: %{y:.1f} m<extra></extra>',
        };

        const layout2dObj = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#94a3b8', size: 11 },
            margin: { l: 52, r: 12, t: 6, b: 40 },
            xaxis: {
                title: { text: mode === 'azimuth' ? 'Azimuth (°)' : 'Dip (°)', font: { color: '#cbd5e1' } },
                gridcolor: 'rgba(148,163,184,0.18)', zerolinecolor: 'rgba(148,163,184,0.3)', color: '#94a3b8',
            },
            yaxis: {
                title: { text: 'Depth (m)', font: { color: '#cbd5e1' } },
                gridcolor: 'rgba(148,163,184,0.18)', zerolinecolor: 'rgba(148,163,184,0.3)', color: '#94a3b8',
                autorange: 'reversed' as const,
            },
            showlegend: false,
        };

        // ── 3D traces + layout ────────────────────────────────────────
        let trace3d: Record<string, unknown> | null = null;
        let layout3dObj: Record<string, unknown> = {};

        if (mode === 'azimuth') {
            // Helix around unit circle; depth on -Z.
            const xs: number[] = [];
            const ys: number[] = [];
            const zs: number[] = [];
            for (const s of scalarPts) {
                const azRad = (s.v * Math.PI) / 180;
                // Convention: 0° = +Y (north), 90° = +X (east) — same as stereonet.
                xs.push(Math.sin(azRad));
                ys.push(Math.cos(azRad));
                zs.push(-s.d);
            }
            trace3d = {
                type: 'scatter3d',
                mode: 'lines+markers',
                x: xs, y: ys, z: zs,
                line: { color: '#22d3ee', width: 4 },
                marker: {
                    size: 4,
                    color: scalarPts.map((p) => p.v),
                    colorscale: 'Viridis',
                    showscale: false,
                },
                text: scalarPts.map((p) => `Az: ${p.v.toFixed(1)}° @ ${p.d.toFixed(1)} m`),
                hoverinfo: 'text',
                name: 'Azimuth helix',
            };
            layout3dObj = {
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                margin: { l: 0, r: 0, t: 10, b: 0 },
                showlegend: false,
                scene: {
                    bgcolor: 'rgba(0,0,0,0)',
                    xaxis: { title: { text: 'East (sin az)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                    yaxis: { title: { text: 'North (cos az)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                    zaxis: { title: { text: 'Depth (m)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                    aspectmode: 'auto' as const,
                    camera: { eye: { x: 1.4, y: 1.4, z: 0.8 } },
                },
            };
        } else {
            // Dip 3D: station-by-station slant. Start at origin, project
            // each MD segment onto a vertical plane in the azimuth
            // direction. Horizontal distance grows by cos|dip|·dMD,
            // vertical drop grows by sin|dip|·dMD.
            const xs: number[] = [0];
            const ys: number[] = [0];
            const zs: number[] = [0];
            let horizAccum = 0;
            let vertAccum = 0;

            // Walk adjacent pairs using full 2D points (we need azimuth
            // to place the slant in 3D space if collarAzimuth is known;
            // otherwise we put everything on the +Y axis).
            for (let i = 1; i < pts.length; i++) {
                const a = pts[i - 1];
                const b = pts[i];
                const dMd = b.depth - a.depth;
                if (dMd <= 0) continue;
                const avgDip = (((a.dip ?? 0) + (b.dip ?? 0)) / 2);
                const avgAz = ((a.azimuth ?? collarAzimuth ?? 0) + (b.azimuth ?? collarAzimuth ?? 0)) / 2;

                const dipRad = (Math.abs(avgDip) * Math.PI) / 180;
                const azRad = (avgAz * Math.PI) / 180;
                const horiz = dMd * Math.cos(dipRad);
                const vert = dMd * Math.sin(dipRad);
                horizAccum += horiz;
                vertAccum += vert;
                xs.push(horizAccum * Math.sin(azRad));
                ys.push(horizAccum * Math.cos(azRad));
                zs.push(-vertAccum);
            }

            trace3d = {
                type: 'scatter3d',
                mode: 'lines+markers',
                x: xs, y: ys, z: zs,
                line: { color: '#22d3ee', width: 4 },
                marker: { size: 4, color: '#22d3ee' },
                text: pts.slice(0, xs.length).map((p) => `Dip: ${(p.dip ?? 0).toFixed(1)}°  Az: ${(p.azimuth ?? 0).toFixed(1)}°  @ ${p.depth.toFixed(1)} m`),
                hoverinfo: 'text',
                name: 'Dip trajectory',
            };
            layout3dObj = {
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                margin: { l: 0, r: 0, t: 10, b: 0 },
                showlegend: false,
                scene: {
                    bgcolor: 'rgba(0,0,0,0)',
                    xaxis: { title: { text: 'East (m)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                    yaxis: { title: { text: 'North (m)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                    zaxis: { title: { text: 'Depth below collar (m)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                    aspectmode: 'data' as const,
                    camera: { eye: { x: 1.4, y: 1.4, z: 0.9 } },
                },
            };
        }

        return {
            data2d: trace2d,
            data3d: trace3d,
            layout2d: layout2dObj,
            layout3d: layout3dObj,
            hasData: true,
        };
    }, [surveys, mode, view, collarValue, collarAzimuth]);

    if (!hasData) {
        return (
            <div className="flex items-center justify-center h-full min-h-[200px] text-gray-500 text-sm">
                Need at least 2 {mode} values to plot.
            </div>
        );
    }

    if (view === '3d' && data3d) {
        return <GeoPlot data={[data3d] as unknown as Record<string, unknown>[]} layout={layout3d} />;
    }
    return <GeoPlot data={[data2d] as unknown as Record<string, unknown>[]} layout={layout2d} />;
}
