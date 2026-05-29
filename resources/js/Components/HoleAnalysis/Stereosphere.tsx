import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';

interface Structure {
    depth: number;
    structure_type: string;
    true_dip: number | null;
    dip_direction: number | null;
    description?: string | null;
}

interface StereosphereProps {
    structures: Structure[];
    holeId: string;
    visibleTypes?: Record<string, boolean>;
}

/**
 * 3-D stereosphere — the lower hemisphere rendered as an actual half-sphere
 * in XYZ space. Planes become true great circles (arcs on the sphere
 * surface) and poles become points sitting on the surface. Users can
 * rotate, zoom, and pan to read the structural geometry directly rather
 * than through a 2-D equal-area projection.
 *
 * Convention:
 *   +X = East      +Y = North      -Z = downward (lower hemisphere)
 * So the dome you see is the bottom half of a unit sphere, flipped so
 * the flat circular "net rim" sits at Z=0 and the pole of the sphere
 * hangs below at Z=-1.
 *
 * This shares the type→color palette with the 2-D Stereonet so users
 * recognise the same bedding / foliation / joint clusters across views.
 */

const TYPE_COLORS: Record<string, string> = {
    bedding:   '#3b82f6',
    foliation: '#a855f7',
    fault:     '#ef4444',
    shear:     '#f97316',
    joint:     '#14b8a6',
    fracture:  '#eab308',
    vein:      '#22c55e',
    lineation: '#ec4899',
};

/**
 * Given a plane's dip direction + true dip, walk its great circle and
 * return XYZ points on the lower-hemisphere unit sphere. Formula:
 *   - For t in [0, π] (rake along the strike line),
 *   - local frame: x=strike, y=horiz-in-dip-dir, z=up
 *   - rotate into geographic frame (East, North, Up)
 *   - filter to lower hemisphere (z ≤ 0, using "down-is-negative")
 */
function planeArc3D(dipDirection: number, dip: number, nSamples = 120): { x: number[]; y: number[]; z: number[] } {
    const strike = (dipDirection - 90 + 360) % 360;
    const dipRad = (dip * Math.PI) / 180;
    const strikeRad = (strike * Math.PI) / 180;
    const dirRad = (dipDirection * Math.PI) / 180;

    // Local frame unit vectors in geographic (East, North, Up) coords.
    // Strike azimuth → direction cosines (East = sin, North = cos).
    const ex = Math.sin(strikeRad);
    const ny = Math.cos(strikeRad);
    // Local +y (horizontal-in-dip-direction).
    const dx = Math.sin(dirRad);
    const dy = Math.cos(dirRad);

    const xs: number[] = [];
    const ys: number[] = [];
    const zs: number[] = [];

    for (let i = 0; i <= nSamples; i++) {
        const t = (i / nSamples) * Math.PI;  // walk one half of the plane
        const lx = Math.cos(t);
        const ly = Math.sin(t) * Math.cos(dipRad);
        const lz = -Math.sin(t) * Math.sin(dipRad);  // down is negative

        // Rotate local → geographic (East, North, Up).
        const E = lx * ex + ly * dx;
        const N = lx * ny + ly * dy;
        const U = lz;  // up is negative for the lower hemisphere

        // Lower hemisphere only (down in Up frame → U ≤ 0). Skip pts above.
        if (U > 0) continue;

        xs.push(E);
        ys.push(N);
        zs.push(U);
    }
    return { x: xs, y: ys, z: zs };
}

/** Pole-to-plane as an XYZ unit vector on the lower hemisphere. */
function pole3D(dipDirection: number, dip: number): { x: number; y: number; z: number } {
    // The pole points downward-opposite-to-dip-direction, at a plunge
    // of (90 − dip) from horizontal. For a dip of 0° (horizontal plane)
    // the pole is straight down (0, 0, -1). For a vertical plane (90°)
    // the pole lies on the horizon opposite the dip direction.
    const poleTrend = (dipDirection + 180) % 360;    // opposite bearing
    const polePlunge = 90 - dip;                     // how far below horizon
    const plungeRad = (polePlunge * Math.PI) / 180;
    const trendRad = (poleTrend * Math.PI) / 180;
    const horiz = Math.cos(plungeRad);
    return {
        x: horiz * Math.sin(trendRad),   // East
        y: horiz * Math.cos(trendRad),   // North
        z: -Math.sin(plungeRad),         // Down (negative Z)
    };
}

/** Same for a lineation (trend + plunge carried as dip_direction + true_dip). */
function lineation3D(trend: number, plunge: number): { x: number; y: number; z: number } {
    const plungeRad = (plunge * Math.PI) / 180;
    const trendRad = (trend * Math.PI) / 180;
    const horiz = Math.cos(plungeRad);
    return {
        x: horiz * Math.sin(trendRad),
        y: horiz * Math.cos(trendRad),
        z: -Math.sin(plungeRad),
    };
}

export default function Stereosphere({ structures, holeId, visibleTypes }: StereosphereProps) {
    const { traces, layout } = useMemo(() => {
        const tracesArr: Record<string, unknown>[] = [];

        // Wireframe hemisphere — a gentle grid at every 30° latitude and
        // longitude so the user can orient themselves. Opaque fill would
        // hide the structures behind, so we use an opaque-ish mesh.
        const latLines: { x: number[]; y: number[]; z: number[] }[] = [];
        const lonLines: { x: number[]; y: number[]; z: number[] }[] = [];
        for (let plunge = 15; plunge < 90; plunge += 15) {
            const r = Math.cos((plunge * Math.PI) / 180);
            const z = -Math.sin((plunge * Math.PI) / 180);
            const xs: number[] = [];
            const ys: number[] = [];
            const zs: number[] = [];
            for (let t = 0; t <= 360; t += 5) {
                const rad = (t * Math.PI) / 180;
                xs.push(r * Math.sin(rad));
                ys.push(r * Math.cos(rad));
                zs.push(z);
            }
            latLines.push({ x: xs, y: ys, z: zs });
        }
        for (let bearing = 0; bearing < 360; bearing += 30) {
            const rad = (bearing * Math.PI) / 180;
            const xs: number[] = [];
            const ys: number[] = [];
            const zs: number[] = [];
            for (let p = 0; p <= 90; p += 5) {
                const pRad = (p * Math.PI) / 180;
                xs.push(Math.cos(pRad) * Math.sin(rad));
                ys.push(Math.cos(pRad) * Math.cos(rad));
                zs.push(-Math.sin(pRad));
            }
            lonLines.push({ x: xs, y: ys, z: zs });
        }

        // Primitive circle (horizon, Z=0).
        const horizonXs: number[] = [];
        const horizonYs: number[] = [];
        for (let t = 0; t <= 360; t += 3) {
            const rad = (t * Math.PI) / 180;
            horizonXs.push(Math.sin(rad));
            horizonYs.push(Math.cos(rad));
        }
        tracesArr.push({
            type: 'scatter3d',
            mode: 'lines',
            x: horizonXs,
            y: horizonYs,
            z: horizonXs.map(() => 0),
            line: { color: 'rgba(148,163,184,0.9)', width: 3 },
            showlegend: false,
            hoverinfo: 'skip',
            name: 'horizon',
        });

        // Latitude rings.
        for (const line of latLines) {
            tracesArr.push({
                type: 'scatter3d',
                mode: 'lines',
                x: line.x,
                y: line.y,
                z: line.z,
                line: { color: 'rgba(148,163,184,0.2)', width: 1 },
                showlegend: false,
                hoverinfo: 'skip',
            });
        }
        // Longitude arcs.
        for (const line of lonLines) {
            tracesArr.push({
                type: 'scatter3d',
                mode: 'lines',
                x: line.x,
                y: line.y,
                z: line.z,
                line: { color: 'rgba(148,163,184,0.2)', width: 1 },
                showlegend: false,
                hoverinfo: 'skip',
            });
        }

        // Group structures by type so each gets a single legend entry +
        // single color. Plotly renders each trace as one object; we
        // concatenate the great-circle arcs for each type with `null`
        // separators so Plotly breaks the line between features.
        const byType: Record<string, Structure[]> = {};
        for (const s of structures) {
            if (!s.true_dip || s.dip_direction == null) continue;
            if (visibleTypes && visibleTypes[s.structure_type] === false) continue;
            byType[s.structure_type] = byType[s.structure_type] || [];
            byType[s.structure_type].push(s);
        }

        for (const [type, rows] of Object.entries(byType)) {
            const color = TYPE_COLORS[type] ?? '#94a3b8';

            if (type === 'lineation') {
                // Lineations as point cloud.
                const pts = rows.map((r) => lineation3D(r.dip_direction!, r.true_dip!));
                tracesArr.push({
                    type: 'scatter3d',
                    mode: 'markers',
                    x: pts.map((p) => p.x),
                    y: pts.map((p) => p.y),
                    z: pts.map((p) => p.z),
                    marker: { size: 5, color, line: { color: 'rgba(0,0,0,0.4)', width: 1 } },
                    name: `${type} (${rows.length})`,
                    text: rows.map((r) => `${type} ${r.true_dip?.toFixed(0)}°/${r.dip_direction?.toFixed(0)}° @ ${r.depth.toFixed(1)}m`),
                    hoverinfo: 'text',
                });
            } else {
                // Planar — great-circle arc trace with null-break separators.
                const xAll: (number | null)[] = [];
                const yAll: (number | null)[] = [];
                const zAll: (number | null)[] = [];
                const poles: { x: number; y: number; z: number; label: string }[] = [];

                for (const r of rows) {
                    const arc = planeArc3D(r.dip_direction!, r.true_dip!);
                    xAll.push(...arc.x, null);
                    yAll.push(...arc.y, null);
                    zAll.push(...arc.z, null);

                    const p = pole3D(r.dip_direction!, r.true_dip!);
                    poles.push({
                        ...p,
                        label: `${type} ${r.true_dip?.toFixed(0)}°/${r.dip_direction?.toFixed(0)}° @ ${r.depth.toFixed(1)}m`,
                    });
                }

                tracesArr.push({
                    type: 'scatter3d',
                    mode: 'lines',
                    x: xAll,
                    y: yAll,
                    z: zAll,
                    line: { color, width: 2.5 },
                    opacity: 0.55,
                    name: `${type} plane (${rows.length})`,
                    hoverinfo: 'skip',
                });

                tracesArr.push({
                    type: 'scatter3d',
                    mode: 'markers',
                    x: poles.map((p) => p.x),
                    y: poles.map((p) => p.y),
                    z: poles.map((p) => p.z),
                    marker: { size: 3.5, color, symbol: 'circle' },
                    name: `${type} pole (${rows.length})`,
                    text: poles.map((p) => p.label),
                    hoverinfo: 'text',
                    showlegend: false,
                });
            }
        }

        // Cardinal direction labels on the horizon.
        const cardinalAnnotations = [
            { showarrow: false, text: 'N', x: 0, y: 1.08, z: 0, font: { color: '#e2e8f0', size: 14 } },
            { showarrow: false, text: 'E', x: 1.08, y: 0, z: 0, font: { color: '#e2e8f0', size: 14 } },
            { showarrow: false, text: 'S', x: 0, y: -1.08, z: 0, font: { color: '#e2e8f0', size: 14 } },
            { showarrow: false, text: 'W', x: -1.08, y: 0, z: 0, font: { color: '#e2e8f0', size: 14 } },
        ];

        const layoutObj = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            margin: { l: 0, r: 0, t: 10, b: 0 },
            showlegend: true,
            legend: {
                font: { color: '#cbd5e1', size: 10 },
                bgcolor: 'rgba(15,23,42,0.6)',
                bordercolor: 'rgba(148,163,184,0.2)',
                borderwidth: 1,
            },
            scene: {
                bgcolor: 'rgba(0,0,0,0)',
                xaxis: { title: { text: 'E', font: { color: '#94a3b8' } }, visible: true, showgrid: false, zeroline: false, showticklabels: false },
                yaxis: { title: { text: 'N', font: { color: '#94a3b8' } }, visible: true, showgrid: false, zeroline: false, showticklabels: false },
                zaxis: { title: { text: 'Depth', font: { color: '#94a3b8' } }, visible: true, showgrid: false, zeroline: false, showticklabels: false },
                aspectmode: 'cube' as const,
                camera: { eye: { x: 1.3, y: 1.3, z: 0.9 } },
                annotations: cardinalAnnotations,
            },
        };

        return { traces: tracesArr, layout: layoutObj };
    }, [structures, visibleTypes]);

    return (
        <div className="flex flex-col h-full" aria-label={`3D stereosphere for drill hole ${holeId}`}>
            <div className="flex-1 min-h-[420px]">
                <GeoPlot data={traces} layout={layout as Record<string, unknown>} />
            </div>
            <div className="text-xs text-gray-400 text-center mt-1">
                3-D lower hemisphere · drag to rotate · scroll to zoom
            </div>
        </div>
    );
}
