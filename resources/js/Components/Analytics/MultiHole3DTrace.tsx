import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';
import { describeDesurvey, desurveyCollars, type PathPoint } from '@/lib/desurvey';

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
    /** Needed to extend a hole past its last station (or an unsurveyed hole) to TD. */
    total_depth?: number | null;
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
 * easting / northing / elevation space. Each hole is desurveyed with minimum
 * curvature (lib/desurvey — the same method as the server-side traces) and
 * extended to TD along its last attitude.
 *
 * Holes with no downhole survey rows are drawn along their collar
 * azimuth/dip, DASHED, with an open-circle collar, and the part of any hole
 * beyond its deepest survey is dashed too — a projection must not look like
 * a measurement (FE-8, 2026-09-29). The caption above the plot says how many
 * of each there are.
 *
 * Purpose: spot drilling-pattern gaps, overlapping targets, and the
 * overall geometry of the drill array relative to the AOI.
 */
export default function MultiHole3DTrace({ collars, surveys, colorBy = 'status' }: Props) {
    const { traces, layout, hasData, caption } = useMemo(() => {
        if (collars.length === 0) return { traces: [], layout: {}, hasData: false, caption: '' };

        const palette = colorBy === 'type' ? TYPE_COLORS : STATUS_COLORS;
        const colorKey = (c: Collar) => (colorBy === 'type' ? c.hole_type : c.status) ?? 'unknown';

        const holes = desurveyCollars(collars, surveys);

        // Group traces by colour key so each category gets one legend row.
        const groups: Record<string, Record<string, unknown>[]> = {};
        let anyProjected = false;

        for (const c of collars) {
            const hole = holes.get(c.collar_id);
            if (!hole) continue;

            const k = colorKey(c);
            const color = palette[k] ?? '#94a3b8';
            const label = `${c.hole_id} — ${c.hole_type ?? '—'} (${c.status ?? 'unknown'})`;
            const unsurveyed = !hole.surveyed;
            const note = unsurveyed
                ? (hole.orientation === 'collar'
                    ? ' · UNSURVEYED — projected on collar az/dip'
                    : ' · NO ORIENTATION — drawn vertical')
                : '';
            const toXYZ = (pts: PathPoint[]) => ({
                x: pts.map((p) => hole.origin.x + p.x),
                y: pts.map((p) => hole.origin.y + p.y),
                z: pts.map((p) => hole.origin.z + p.z),
            });

            // Measured part solid; projection beyond the last station dashed.
            const firstExtra = hole.path.findIndex((p) => p.extrapolated);
            const measured = firstExtra === -1 ? hole.path : hole.path.slice(0, firstExtra);
            const projected = firstExtra > 0 ? hole.path.slice(firstExtra - 1) : [];

            if (!unsurveyed && measured.length >= 2) {
                (groups[k] = groups[k] || []).push({
                    type: 'scatter3d',
                    mode: 'lines',
                    ...toXYZ(measured),
                    line: { color, width: 3 },
                    hovertext: label,
                    hoverinfo: 'text',
                    name: k,
                    showlegend: false,
                });
            }
            const dashed = unsurveyed ? hole.path : projected;
            if (dashed.length >= 2) {
                anyProjected = true;
                (groups[k] = groups[k] || []).push({
                    type: 'scatter3d',
                    mode: 'lines',
                    ...toXYZ(dashed),
                    line: { color, width: 2, dash: 'dash' },
                    opacity: 0.7,
                    hovertext: `${label}${note || ' · beyond last survey — projected to TD'}`,
                    hoverinfo: 'text',
                    name: k,
                    showlegend: false,
                });
            }

            // Collar marker — diamond; an unsurveyed hole's collar is an open
            // circle so it reads differently even end-on.
            (groups[k] = groups[k] || []).push({
                type: 'scatter3d',
                mode: 'markers',
                x: [hole.origin.x], y: [hole.origin.y], z: [hole.origin.z],
                marker: unsurveyed
                    ? { size: 4, color: 'rgba(0,0,0,0)', symbol: 'circle-open', line: { color, width: 2 } }
                    : { size: 4, color, symbol: 'diamond', line: { color: 'rgba(0,0,0,0.4)', width: 1 } },
                hovertext: `${c.hole_id} collar${note}`,
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
        if (anyProjected) {
            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: [null], y: [null], z: [null],
                line: { color: '#94a3b8', width: 2, dash: 'dash' },
                name: 'projected (no survey)',
                showlegend: true,
                hoverinfo: 'skip',
            });
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

        return { traces, layout, hasData: holes.size > 0, caption: describeDesurvey(holes.values()) };
    }, [collars, surveys, colorBy]);

    if (!hasData) {
        return <div className="flex items-center justify-center h-full text-sm text-gray-500">No collars to plot.</div>;
    }
    return (
        <div className="flex flex-col h-full min-h-0">
            <div className="text-[10px] font-mono mb-1 shrink-0" style={{ color: 'var(--fg-3)' }} data-testid="desurvey-caption">
                {caption}
            </div>
            <div className="flex-1 min-h-0">
                <GeoPlot data={traces} layout={layout as Record<string, unknown>} />
            </div>
        </div>
    );
}
