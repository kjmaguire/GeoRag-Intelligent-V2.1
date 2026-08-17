import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';

interface Collar {
    collar_id: string;
    hole_id: string;
    azimuth: number | null;
    dip: number | null;
    elevation: number | null;
    easting: number | null;
    northing: number | null;
    hole_type: string | null;
    status: string | null;
}

interface Survey { collar_id: string; depth: number; azimuth: number | null; dip: number | null; }

interface Props {
    collars: Collar[];
    surveys: Survey[];
    /** 'status' colours by status (green/amber/red), 'type' by hole type (sky/purple). */
    colorBy?: 'status' | 'type';
}

const STATUS_COLORS: Record<string, string> = {
    Completed:     '#22c55e',
    'In Progress': '#eab308',
    Active:        '#eab308',
    Abandoned:     '#ef4444',
};

const TYPE_COLORS: Record<string, string> = {
    Diamond: '#38bdf8',
    RC:      '#a855f7',
    RAB:     '#ec4899',
};

/**
 * Render every drill hole in the project as a 3-D polyline in shared
 * UTM-ish space (easting / northing / elevation). Each hole is
 * integrated from its collar + survey stations using the same min-
 * curvature-style step we use in the per-hole OrientationSpiral, then
 * placed at the collar's geographic coords so the whole campaign
 * appears in one rotatable scene.
 *
 * Purpose: spot drilling-pattern gaps, overlapping targets, and the
 * overall geometry of the drill array relative to the AOI.
 */
export default function MultiHole3DTrace({ collars, surveys, colorBy = 'status' }: Props) {
    const { traces, layout, hasData } = useMemo(() => {
        if (collars.length === 0) return { traces: [], layout: {}, hasData: false };

        // Index surveys by collar_id for fast lookup.
        const surveysByCollar: Record<string, Survey[]> = {};
        for (const s of surveys) {
            (surveysByCollar[s.collar_id] = surveysByCollar[s.collar_id] || []).push(s);
        }

        const palette = colorBy === 'type' ? TYPE_COLORS : STATUS_COLORS;
        const colorKey = (c: Collar) => (colorBy === 'type' ? c.hole_type : c.status) ?? 'unknown';

        // Group traces by colour key so each category gets one legend row.
        const groups: Record<string, Record<string, unknown>[]> = {};

        for (const c of collars) {
            if (c.easting == null || c.northing == null) continue;
            const elev0 = c.elevation ?? 0;

            // Build station list (collar + surveys), ordered by depth.
            const stations: { depth: number; azimuth: number; dip: number }[] = [];
            if (c.azimuth != null && c.dip != null) {
                stations.push({ depth: 0, azimuth: c.azimuth, dip: c.dip });
            }
            const ownSurveys = (surveysByCollar[c.collar_id] || [])
                .filter((s) => s.azimuth != null && s.dip != null);
            for (const s of ownSurveys) {
                stations.push({ depth: s.depth, azimuth: s.azimuth!, dip: s.dip! });
            }
            stations.sort((a, b) => a.depth - b.depth);

            // Accumulate XYZ offsets from the collar.
            const xs: number[] = [c.easting];
            const ys: number[] = [c.northing];
            const zs: number[] = [elev0];

            for (let i = 1; i < stations.length; i++) {
                const a = stations[i - 1];
                const b = stations[i];
                const dMd = b.depth - a.depth;
                if (dMd <= 0) continue;
                const avgAz = 0.5 * (a.azimuth + b.azimuth);
                const avgDip = 0.5 * (a.dip + b.dip);
                const azRad = (avgAz * Math.PI) / 180;
                const dipRad = (Math.abs(avgDip) * Math.PI) / 180;
                const horiz = dMd * Math.cos(dipRad);
                const vert  = dMd * Math.sin(dipRad);
                xs.push(xs[xs.length - 1] + horiz * Math.sin(azRad));
                ys.push(ys[ys.length - 1] + horiz * Math.cos(azRad));
                zs.push(zs[zs.length - 1] - vert);
            }

            const k = colorKey(c);
            const color = palette[k] ?? '#94a3b8';

            (groups[k] = groups[k] || []).push({
                type: 'scatter3d',
                mode: 'lines',
                x: xs, y: ys, z: zs,
                line: { color, width: 3 },
                hovertext: `${c.hole_id} — ${c.hole_type ?? '—'} (${c.status ?? 'unknown'})`,
                hoverinfo: 'text',
                name: k,
                showlegend: false,  // consolidated legend via markers below
            });

            // Start marker at the collar — small diamond.
            (groups[k] = groups[k] || []).push({
                type: 'scatter3d',
                mode: 'markers',
                x: [xs[0]], y: [ys[0]], z: [zs[0]],
                marker: { size: 4, color, symbol: 'diamond', line: { color: 'rgba(0,0,0,0.4)', width: 1 } },
                hovertext: `${c.hole_id} collar`,
                hoverinfo: 'text',
                showlegend: false,
            });
        }

        const traces: Record<string, unknown>[] = [];
        for (const k of Object.keys(groups)) {
            // Inject one invisible marker per group with showlegend:true,
            // so the legend lists statuses/types without duplicating
            // every hole.
            traces.push({
                type: 'scatter3d',
                mode: 'markers',
                x: [null], y: [null], z: [null],
                marker: { size: 8, color: palette[k] ?? '#94a3b8' },
                name: k,
                showlegend: true,
                hoverinfo: 'skip',
            });
            for (const t of groups[k]) traces.push(t);
        }

        const layout = {
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
                xaxis: { title: { text: 'Easting (m)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                yaxis: { title: { text: 'Northing (m)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                zaxis: { title: { text: 'Elevation (m)', font: { color: '#94a3b8' } }, color: '#94a3b8', gridcolor: 'rgba(148,163,184,0.15)' },
                aspectmode: 'data' as const,
                camera: { eye: { x: 1.4, y: 1.4, z: 0.8 } },
            },
        };

        return { traces, layout, hasData: true };
    }, [collars, surveys, colorBy]);

    if (!hasData) {
        return <div className="flex items-center justify-center h-full text-sm text-gray-500">No collars to plot.</div>;
    }
    return <GeoPlot data={traces} layout={layout as Record<string, unknown>} />;
}
