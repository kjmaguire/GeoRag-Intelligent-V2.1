import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';
import { desurveyHole, type OrientationSource } from '@/lib/desurvey';

interface Survey {
    depth: number;
    azimuth: number | null;
    dip: number | null;
    survey_method?: string | null;
}

interface OrientationSpiralProps {
    surveys: Survey[];
    collarAzimuth: number | null;
    collarDip: number | null;
    collarElevation: number | null;
    totalDepth: number | null;
    /**
     * '3d' = the rotatable 3-D spiral (default, shows trajectory shape).
     * '2d' = side-by-side Plan (top-down) + Section (along-azimuth) views,
     *        the same pair geologists draw on paper in NI 43-101 reports.
     *        Plan answers "where horizontally did the bit end up?",
     *        Section answers "did the hole descend on-gradient?"
     */
    view?: '2d' | '3d';
}

interface Trajectory {
    /** One entry per path point: measured depth + attitude there. */
    pts: { depth: number; azimuth: number; dip: number }[];
    eOffset: number[];
    nOffset: number[];
    zDrop: number[];
    zElev: number[];
    alongHoleHoriz: number[];  // cumulative horizontal distance from collar
    elev: number;
    hasData: boolean;
    surveyed: boolean;
    orientation: OrientationSource;
}

/**
 * Desurvey the hole with the shared minimum-curvature implementation
 * (lib/desurvey) and derive the arrays the Plan / Section / 3D renders use.
 *
 * This used to average the two stations' azimuths linearly — 355° and 5°
 * gave 180°, drawing that segment backwards — and needed two stations, so a
 * collar-only hole plotted nothing. It now extends to TD and reports whether
 * the path came from surveys at all (FE-8, 2026-09-29).
 */
export function buildTrajectory(
    surveys: Survey[],
    collarAzimuth: number | null,
    collarDip: number | null,
    collarElevation: number | null,
    totalDepth: number | null = null,
): Trajectory {
    const hole = desurveyHole({ azimuth: collarAzimuth, dip: collarDip, totalDepth }, surveys);
    const pts = hole.path.map((p) => ({ depth: p.md, azimuth: p.azimuth, dip: p.dip }));
    const eOffset = hole.path.map((p) => p.x);
    const nOffset = hole.path.map((p) => p.y);
    const zDrop = hole.path.map((p) => p.z);
    const alongHoleHoriz: number[] = [0];
    for (let i = 1; i < hole.path.length; i++) {
        const a = hole.path[i - 1];
        const b = hole.path[i];
        alongHoleHoriz.push(alongHoleHoriz[i - 1] + Math.hypot(b.x - a.x, b.y - a.y));
    }

    const elev = collarElevation ?? 0;
    const zElev = zDrop.map((z) => elev + z);

    return {
        pts,
        eOffset,
        nOffset,
        zDrop,
        zElev,
        alongHoleHoriz,
        elev,
        // A path needs length; a collar attitude + TD is enough.
        hasData: hole.path.length >= 2 && hole.orientation !== 'assumed_vertical',
        surveyed: hole.surveyed,
        orientation: hole.orientation,
    };
}

export default function OrientationSpiral({
    surveys,
    collarAzimuth,
    collarDip,
    collarElevation,
    totalDepth,
    view = '3d',
}: OrientationSpiralProps) {
    const traj = useMemo(
        () => buildTrajectory(surveys, collarAzimuth, collarDip, collarElevation, totalDepth),
        [surveys, collarAzimuth, collarDip, collarElevation, totalDepth],
    );

    if (!traj.hasData) {
        return (
            <div className="flex items-center justify-center h-full min-h-[280px] text-gray-500 text-sm">
                Need survey stations, or a collar azimuth + dip with a total depth, to plot a trajectory.
            </div>
        );
    }

    const body = view === '2d'
        ? <TwoDViews traj={traj} totalDepth={totalDepth} collarElevation={collarElevation} />
        : <ThreeDView traj={traj} totalDepth={totalDepth} collarElevation={collarElevation} />;

    if (traj.surveyed) return body;
    return (
        <div className="flex flex-col h-full min-h-0">
            <div
                role="note"
                data-testid="spiral-unsurveyed"
                className="text-[10px] font-mono mb-1 px-2 py-1 rounded border shrink-0"
                style={{ color: '#f59e0b', borderColor: '#f59e0b55' }}
            >
                UNSURVEYED — no downhole survey rows; the path is projected straight along the collar azimuth/dip to TD.
            </div>
            <div className="flex-1 min-h-0">{body}</div>
        </div>
    );
}

// ── 3-D view (unchanged from previous implementation) ─────────────────

interface SubProps {
    traj: Trajectory;
    totalDepth: number | null;
    collarElevation: number | null;
}

function ThreeDView({ traj, totalDepth, collarElevation }: SubProps) {
    const { traces, layout } = useMemo(() => {
        const { pts, eOffset, nOffset, zElev, zDrop } = traj;

        const hoverText = nOffset.map((_, i) =>
            `Depth: ${(-zDrop[i]).toFixed(1)} m<br>Elev: ${zElev[i].toFixed(1)} m<br>Az: ${pts[Math.min(i, pts.length - 1)].azimuth.toFixed(1)}°<br>Dip: ${pts[Math.min(i, pts.length - 1)].dip.toFixed(1)}°`
        );

        const trajectory = {
            type: 'scatter3d',
            mode: 'lines+markers',
            x: eOffset,
            y: nOffset,
            z: zElev,
            line: { color: '#22d3ee', width: 4 },
            marker: { size: 4, color: '#22d3ee' },
            name: 'Trajectory',
            text: hoverText,
            hoverinfo: 'text',
        };

        const collarMarker = {
            type: 'scatter3d',
            mode: 'markers',
            x: [0], y: [0], z: [traj.elev],
            marker: { size: 8, color: '#22c55e', symbol: 'diamond' },
            name: 'Collar',
            hovertext: 'Collar (0, 0)',
            hoverinfo: 'text',
        };

        const eohMarker = {
            type: 'scatter3d',
            mode: 'markers',
            x: [eOffset[eOffset.length - 1]],
            y: [nOffset[nOffset.length - 1]],
            z: [zElev[zElev.length - 1]],
            marker: { size: 8, color: '#ef4444', symbol: 'circle' },
            name: 'End of hole',
            hovertext: `EOH at ${(totalDepth ?? -zDrop[zDrop.length - 1]).toFixed(1)} m`,
            hoverinfo: 'text',
        };

        const layoutObj = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            margin: { l: 0, r: 0, t: 10, b: 0 },
            showlegend: false,
            scene: {
                bgcolor: 'rgba(0,0,0,0)',
                xaxis: {
                    title: { text: 'E-W offset (m)', font: { color: '#94a3b8' } },
                    color: '#94a3b8',
                    gridcolor: 'rgba(148,163,184,0.15)',
                    zerolinecolor: 'rgba(148,163,184,0.3)',
                },
                yaxis: {
                    title: { text: 'N-S offset (m)', font: { color: '#94a3b8' } },
                    color: '#94a3b8',
                    gridcolor: 'rgba(148,163,184,0.15)',
                    zerolinecolor: 'rgba(148,163,184,0.3)',
                },
                zaxis: {
                    title: { text: collarElevation != null ? 'Elevation (m)' : 'Depth (m)', font: { color: '#94a3b8' } },
                    color: '#94a3b8',
                    gridcolor: 'rgba(148,163,184,0.15)',
                    zerolinecolor: 'rgba(148,163,184,0.3)',
                },
                aspectmode: 'data' as const,
                camera: { eye: { x: 1.25, y: 1.25, z: 0.9 } },
            },
        };

        return {
            traces: [trajectory, collarMarker, eohMarker] as Record<string, unknown>[],
            layout: layoutObj,
        };
    }, [traj, totalDepth, collarElevation]);

    return (
        <GeoPlot data={traces} layout={layout as Record<string, unknown>} />
    );
}

// ── 2-D Plan + Section (side-by-side, NI 43-101 standard layout) ─────

function TwoDViews({ traj, totalDepth, collarElevation }: SubProps) {
    const plan = useMemo(() => buildPlan(traj, totalDepth), [traj, totalDepth]);
    const section = useMemo(() => buildSection(traj, totalDepth, collarElevation), [traj, totalDepth, collarElevation]);

    return (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 h-full">
            <div className="bg-gray-900/40 rounded border border-gray-800 p-3 min-h-[360px]">
                <div className="text-xs font-medium text-gray-300 mb-1">Plan view</div>
                <div className="text-[10px] text-gray-500 mb-2">Top-down · north up · collar at origin</div>
                <div className="h-[360px]">
                    <GeoPlot data={plan.traces as unknown as Record<string, unknown>[]} layout={plan.layout} />
                </div>
            </div>
            <div className="bg-gray-900/40 rounded border border-gray-800 p-3 min-h-[360px]">
                <div className="text-xs font-medium text-gray-300 mb-1">Section view</div>
                <div className="text-[10px] text-gray-500 mb-2">Along hole azimuth · depth positive down</div>
                <div className="h-[360px]">
                    <GeoPlot data={section.traces as unknown as Record<string, unknown>[]} layout={section.layout} />
                </div>
            </div>
        </div>
    );
}

function buildPlan(traj: Trajectory, totalDepth: number | null) {
    const { eOffset, nOffset, pts, zDrop } = traj;
    const hoverText = eOffset.map((_, i) =>
        `Depth: ${(-zDrop[i]).toFixed(1)} m<br>E: ${eOffset[i].toFixed(1)} m · N: ${nOffset[i].toFixed(1)} m<br>Az: ${pts[Math.min(i, pts.length - 1)].azimuth.toFixed(1)}°`
    );

    const trajectoryTrace = {
        type: 'scatter',
        mode: 'lines+markers',
        x: eOffset,
        y: nOffset,
        line: { color: '#22d3ee', width: 2 },
        marker: {
            size: 5,
            color: zDrop,           // colour the station markers by depth so
            colorscale: 'Viridis',  // the reader sees direction-of-travel
            showscale: false,
            reversescale: true,
        },
        text: hoverText,
        hoverinfo: 'text',
        name: 'Trajectory',
    };

    const collarTrace = {
        type: 'scatter',
        mode: 'markers',
        x: [0], y: [0],
        marker: { size: 11, color: '#22c55e', symbol: 'diamond', line: { color: '#14532d', width: 1 } },
        name: 'Collar',
        hovertext: 'Collar (0, 0)',
        hoverinfo: 'text',
    };

    const eohTrace = {
        type: 'scatter',
        mode: 'markers',
        x: [eOffset[eOffset.length - 1]],
        y: [nOffset[nOffset.length - 1]],
        marker: { size: 10, color: '#ef4444', symbol: 'circle', line: { color: '#7f1d1d', width: 1 } },
        name: 'End of hole',
        hovertext: `EOH at ${(totalDepth ?? -zDrop[zDrop.length - 1]).toFixed(1)} m`,
        hoverinfo: 'text',
    };

    const layoutObj = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#94a3b8', size: 11 },
        margin: { l: 52, r: 12, t: 6, b: 40 },
        xaxis: {
            title: { text: 'E-W offset (m)', font: { color: '#cbd5e1' } },
            gridcolor: 'rgba(148,163,184,0.18)',
            zerolinecolor: 'rgba(148,163,184,0.4)',
            color: '#94a3b8',
        },
        yaxis: {
            title: { text: 'N-S offset (m)', font: { color: '#cbd5e1' } },
            gridcolor: 'rgba(148,163,184,0.18)',
            zerolinecolor: 'rgba(148,163,184,0.4)',
            color: '#94a3b8',
            scaleanchor: 'x' as const,    // equal aspect ratio — 1 m east = 1 m north on screen
            scaleratio: 1,
        },
        showlegend: false,
        annotations: [
            { showarrow: false, text: 'N', x: 0, y: 1.02, xref: 'paper', yref: 'paper', font: { color: '#cbd5e1', size: 14 } },
        ],
    };

    return {
        traces: [trajectoryTrace, collarTrace, eohTrace],
        layout: layoutObj,
    };
}

function buildSection(traj: Trajectory, totalDepth: number | null, collarElevation: number | null) {
    const { alongHoleHoriz, zDrop, zElev, pts } = traj;
    const hoverText = alongHoleHoriz.map((_, i) =>
        `MD: ~${pts[Math.min(i, pts.length - 1)].depth.toFixed(1)} m<br>Horiz: ${alongHoleHoriz[i].toFixed(1)} m<br>Depth: ${(-zDrop[i]).toFixed(1)} m<br>Dip: ${pts[Math.min(i, pts.length - 1)].dip.toFixed(1)}°`
    );

    const showElev = collarElevation != null;
    const yValues = showElev ? zElev : zDrop.map((z) => -z);  // depth positive downward

    const trajectoryTrace = {
        type: 'scatter',
        mode: 'lines+markers',
        x: alongHoleHoriz,
        y: yValues,
        line: { color: '#22d3ee', width: 2 },
        marker: {
            size: 5,
            color: zDrop,
            colorscale: 'Viridis',
            showscale: false,
            reversescale: true,
        },
        text: hoverText,
        hoverinfo: 'text',
        name: 'Trajectory',
    };

    const collarTrace = {
        type: 'scatter',
        mode: 'markers',
        x: [0], y: [showElev ? traj.elev : 0],
        marker: { size: 11, color: '#22c55e', symbol: 'diamond', line: { color: '#14532d', width: 1 } },
        name: 'Collar',
        hovertext: 'Collar',
        hoverinfo: 'text',
    };

    const eohTrace = {
        type: 'scatter',
        mode: 'markers',
        x: [alongHoleHoriz[alongHoleHoriz.length - 1]],
        y: [yValues[yValues.length - 1]],
        marker: { size: 10, color: '#ef4444', symbol: 'circle', line: { color: '#7f1d1d', width: 1 } },
        name: 'End of hole',
        hovertext: `EOH at ${(totalDepth ?? -zDrop[zDrop.length - 1]).toFixed(1)} m`,
        hoverinfo: 'text',
    };

    const layoutObj = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#94a3b8', size: 11 },
        margin: { l: 52, r: 12, t: 6, b: 40 },
        xaxis: {
            title: { text: 'Along-hole horizontal distance (m)', font: { color: '#cbd5e1' } },
            gridcolor: 'rgba(148,163,184,0.18)',
            zerolinecolor: 'rgba(148,163,184,0.4)',
            color: '#94a3b8',
        },
        yaxis: {
            title: {
                text: showElev ? 'Elevation (m)' : 'Depth below collar (m)',
                font: { color: '#cbd5e1' },
            },
            gridcolor: 'rgba(148,163,184,0.18)',
            zerolinecolor: 'rgba(148,163,184,0.4)',
            color: '#94a3b8',
            autorange: showElev ? true : ('reversed' as const),
        },
        showlegend: false,
    };

    return {
        traces: [trajectoryTrace, collarTrace, eohTrace],
        layout: layoutObj,
    };
}
