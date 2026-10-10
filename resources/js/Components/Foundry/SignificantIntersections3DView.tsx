import { useMemo, useState } from 'react';
import GeoPlot from '@/Components/GeoPlot';
import {
    EAST_AXIS_TITLE,
    NORTH_AXIS_TITLE,
    buildScene3D,
    deepestIntervalByCollar,
    hasLonLat,
    sceneZAxisTitle,
    type SurveyStationInput,
} from '@/lib/desurvey';

interface Intersection {
    collar_id: string;
    element: string;
    cutoff_grade: number;
    from_depth: number;
    to_depth: number;
    true_width_m: number | null;
    weighted_avg: number;
    unit: string;
    peak_value: number | null;
    peak_depth: number | null;
    zone_name: string | null;
}

interface CollarPoint {
    collar_id: string;
    hole_id: string;
    hole_id_canonical: string;
    /** EPSG:4326 position (geom_4326): what a hole is placed by. */
    lng?: number | null;
    lat?: number | null;
    /** As stored, in the CRS of the collar's own upload: not used for placement. */
    easting: number | null;
    northing: number | null;
    total_depth: number | null;
    azimuth?: number | null;
    dip?: number | null;
    elevation?: number | null;
}

/**
 * SignificantIntersections3DView — ghost-rendered hole traces with each
 * cutoff-grade intersection drawn as a glowing thick segment at its
 * downhole position. Holes are desurveyed (lib/desurvey, minimum curvature,
 * extended to TD) and placed at collar elevation — they used to be vertical
 * sticks hung from z = 0 at `-measured depth`, which put a 300 m hit in a
 * -50° hole ~190 m from where it is (FE-9). Element filter lives in the card body so users can sweep
 * Au / Cu / U₃O₈ targeted intervals one element at a time.
 */
export default function SignificantIntersections3DView({
    collars,
    surveys = [],
    intersections,
    height = 560,
}: {
    collars: CollarPoint[];
    surveys?: Array<SurveyStationInput & { collar_id: string }>;
    intersections: Intersection[];
    height?: number;
}) {
    const elementOptions = useMemo(() => {
        const counts = new Map<string, number>();
        for (const it of intersections) counts.set(it.element, (counts.get(it.element) ?? 0) + 1);
        return Array.from(counts.entries())
            .sort((a, b) => b[1] - a[1])
            .map(([element, count]) => ({ element, count }));
    }, [intersections]);

    const [selected, setSelected] = useState<string>(() => elementOptions[0]?.element ?? '');

    const filtered = useMemo(() => intersections.filter((it) => it.element === selected), [intersections, selected]);

    const { data, layout, peakSummary, caption } = useMemo(() => {
        // Placed by lng/lat in one local frame; a collar without one is counted
        // in the caption, not drawn at its per-upload-CRS easting/northing.
        const valid = collars.filter(hasLonLat);
        if (valid.length === 0) {
            return {
                data: [] as Record<string, unknown>[],
                layout: {} as Record<string, unknown>,
                peakSummary: null as null | { min: number; max: number; unit: string },
                caption: '',
            };
        }

        const scene = buildScene3D(
            valid,
            surveys,
            deepestIntervalByCollar(intersections),
            collars.length - valid.length,
        );

        const peaks = filtered.map((it) => it.weighted_avg);
        const pMin = peaks.length > 0 ? Math.min(...peaks) : 0;
        const pMax = peaks.length > 0 ? Math.max(...peaks) : 0;
        const unit = filtered[0]?.unit ?? '';

        // 4-stop heat palette — cool baseline → red hot for the strongest hits.
        const palette = ['#fbbf24', '#f97316', '#ef4444', '#b91c1c'];
        const colorFor = (v: number): string => {
            if (pMax <= pMin) return palette[palette.length - 1];
            const t = (v - pMin) / (pMax - pMin);
            const idx = Math.min(palette.length - 1, Math.max(0, Math.floor(t * (palette.length - 1))));
            return palette[idx];
        };

        const traces: Record<string, unknown>[] = [];

        const intersByCollar = new Map<string, Intersection[]>();
        for (const it of filtered) {
            const arr = intersByCollar.get(it.collar_id) ?? [];
            arr.push(it);
            intersByCollar.set(it.collar_id, arr);
        }

        valid.forEach((collar) => {
            const path = scene.fullPath(collar.collar_id);
            const top = scene.at(collar.collar_id, 0);
            if (!path || !top) return;

            const hits = intersByCollar.get(collar.collar_id) ?? [];
            const isHit = hits.length > 0;

            // Faint ghost trace for the entire hole. Hit holes get a slightly
            // brighter ghost to lift them out of the background visually.
            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                ...path,
                line: { color: isHit ? 'rgba(255,255,255,0.20)' : 'rgba(155,169,184,0.12)', width: isHit ? 2.5 : 1.5 },
                hoverinfo: 'name',
                showlegend: false,
                name: collar.hole_id_canonical || collar.hole_id,
            });

            // Collar dot.
            traces.push({
                type: 'scatter3d',
                mode: 'markers',
                x: [top.x],
                y: [top.y],
                z: [top.z],
                marker: { size: 3.5, color: isHit ? '#facc15' : '#64748b', line: { color: '#0a0e14', width: 1 } },
                hoverinfo: 'name',
                showlegend: false,
                name: collar.hole_id_canonical || collar.hole_id,
            });

            for (const it of hits) {
                const col = colorFor(it.weighted_avg);
                const seg = scene.segment(collar.collar_id, it.from_depth, it.to_depth);
                if (!seg) continue;
                const desc = `${collar.hole_id_canonical || collar.hole_id} · ${it.from_depth.toFixed(1)}–${it.to_depth.toFixed(1)} m · ${it.weighted_avg.toFixed(3)} ${it.unit} (cutoff ${it.cutoff_grade}${it.unit})${it.true_width_m !== null ? ` · TW ${it.true_width_m.toFixed(1)} m` : ''}${it.peak_value !== null ? ` · peak ${it.peak_value.toFixed(3)} @ ${it.peak_depth?.toFixed(1)} m` : ''}${it.zone_name ? ` · ${it.zone_name}` : ''}`;
                traces.push({
                    type: 'scatter3d',
                    mode: 'lines',
                    ...seg,
                    line: { color: col, width: 12 },
                    text: seg.x.map(() => desc),
                    hoverinfo: 'text',
                    showlegend: false,
                    name: 'intersection',
                });

                // Peak-grade marker.
                const peakAt = it.peak_depth !== null ? scene.at(collar.collar_id, it.peak_depth) : null;
                if (it.peak_value !== null && it.peak_depth !== null && peakAt) {
                    traces.push({
                        type: 'scatter3d',
                        mode: 'markers',
                        x: [peakAt.x],
                        y: [peakAt.y],
                        z: [peakAt.z],
                        marker: { size: 5, color: '#fff', line: { color: col, width: 2 } },
                        hoverinfo: 'text',
                        text: [`peak ${it.peak_value.toFixed(3)} ${it.unit} @ ${it.peak_depth.toFixed(1)} m`],
                        showlegend: false,
                        name: 'peak',
                    });
                }
            }
        });

        const layoutObj: Record<string, unknown> = {
            scene: {
                xaxis: {
                    title: { text: EAST_AXIS_TITLE, font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                yaxis: {
                    title: { text: NORTH_AXIS_TITLE, font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                zaxis: {
                    title: { text: sceneZAxisTitle(scene), font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                bgcolor: '#0a0e14',
                // True scale on all three axes: a 1:1:0.6 box drew every dip
                // steeper or flatter than it is (GIS audit 2026-10).
                aspectmode: 'data',
                camera: { eye: { x: 1.6, y: 1.6, z: 0.8 }, up: { x: 0, y: 0, z: 1 } },
            },
            paper_bgcolor: '#0a0e14',
            plot_bgcolor: '#0a0e14',
            margin: { l: 0, r: 0, t: 0, b: 0 },
            showlegend: false,
            hovermode: 'closest',
        };

        return {
            data: traces,
            layout: layoutObj,
            peakSummary: peaks.length > 0 ? { min: pMin, max: pMax, unit } : null,
            caption: scene.caption,
        };
    }, [collars, surveys, intersections, filtered]);

    if (elementOptions.length === 0) {
        return (
            <div className="text-[11px] font-mono p-6 text-center" style={{ color: 'var(--fg-3)' }}>
                No significant intersections yet — they are computed after assays are ingested.
            </div>
        );
    }

    return (
        <div className="flex flex-col h-full min-h-0">
            <div className="flex items-center gap-3 mb-2 shrink-0 flex-wrap">
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                    Element
                </span>
                <select
                    aria-label="Element to plot"
                    value={selected}
                    onChange={(e) => setSelected(e.target.value)}
                    className="text-[11px] font-mono px-2 py-1 rounded border"
                    style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
                >
                    {elementOptions.map((el) => (
                        <option key={el.element} value={el.element}>
                            {el.element} ({el.count})
                        </option>
                    ))}
                </select>
                <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>
                    {filtered.length} intersections
                    {peakSummary &&
                        ` · WAvg ${peakSummary.min.toFixed(3)}–${peakSummary.max.toFixed(3)} ${peakSummary.unit}`}
                </span>
                <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }} data-testid="desurvey-caption">
                    {caption}
                </span>
            </div>
            <div className="flex-1 min-h-0" style={{ height }}>
                <GeoPlot data={data} layout={layout} />
            </div>
        </div>
    );
}
