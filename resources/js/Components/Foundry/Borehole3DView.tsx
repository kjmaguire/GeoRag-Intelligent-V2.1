import { useMemo } from 'react';
import GeoPlot from '@/Components/GeoPlot';
import {
    EAST_AXIS_TITLE,
    NORTH_AXIS_TITLE,
    buildScene3D,
    sceneZAxisTitle,
    hasLonLat,
    type CollarForDesurvey,
    type SurveyStationInput,
    SCENE_3D_ASPECT,
} from '@/lib/desurvey';

interface IntervalBand {
    from: number;
    to: number;
    code: string;
    color: string;
}

interface HoleIntervalRow {
    /** Present since FE-9; joins the row to its collar attitude + surveys. */
    collar_id?: string;
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
 * Each hole is desurveyed (lib/desurvey: minimum curvature from its
 * surveys, or projected along the collar azimuth/dip when it has none) and
 * drawn in 3D space:
 *   x = metres east of the project centroid, from the collar's lng/lat
 *   y = metres north of the project centroid, from the collar's lng/lat
 *   z = collar elevation + desurveyed vertical offset (m)
 * at true scale (aspectmode 'data'), so a drawn dip or azimuth is the real
 * one. The stored easting/northing are in each upload's own CRS and are not
 * used for placement (GIS audit 2026-10). It used to be a vertical line hung
 * from z = 0 whatever the hole's attitude (FE-9).
 *
 * Lithology bands paint each segment via line.color array. ORE bands
 * carry the bright green from the LOGS palette so the visual identity
 * is consistent.
 */
export function Borehole3DView({
    holes,
    collars = [],
    surveys = [],
    height = 560,
}: {
    holes: HoleIntervalRow[];
    /** Collar attitude + elevation, matched on collar_id (or hole id). */
    collars?: Array<CollarForDesurvey & { hole_id?: string; hole_id_canonical?: string }>;
    surveys?: Array<SurveyStationInput & { collar_id: string }>;
    height?: number;
}) {
    const { data, layout, caption } = useMemo(() => {
        // total_depth is optional (§04e, 2026-09-29): a hole without one is
        // still drawn, to its deepest band (`deepest` below, via extendTo).
        // A hole is placed from its lng/lat; one without is counted in the
        // caption, not drawn at its stored (per-upload-CRS) easting/northing.
        const withBands = holes.filter((h) => h.bands.length > 0);
        const valid = withBands.filter(hasLonLat);
        if (valid.length === 0) {
            return { data: [] as Record<string, unknown>[], layout: {} as Record<string, unknown>, caption: '' };
        }

        const byId = new Map<string, CollarForDesurvey>();
        for (const c of collars) {
            byId.set(c.collar_id, c);
            if (c.hole_id_canonical) byId.set(`hole:${c.hole_id_canonical}`, c);
            if (c.hole_id) byId.set(`hole:${c.hole_id}`, c);
        }
        const keyOf = (h: HoleIntervalRow) => h.collar_id ?? `hole:${h.hole_id}`;
        const rows: CollarForDesurvey[] = valid.map((h) => {
            const c = byId.get(keyOf(h)) ?? byId.get(`hole:${h.hole_id}`);
            return {
                collar_id: keyOf(h),
                lng: h.lng,
                lat: h.lat,
                easting: h.easting,
                northing: h.northing,
                total_depth: h.total_depth,
                elevation: c?.elevation ?? null,
                elevation_source: c?.elevation_source ?? null,
                azimuth: c?.azimuth ?? null,
                azimuth_unapplied: c?.azimuth_unapplied ?? null,
                dip: c?.dip ?? null,
            };
        });
        // Surveys are keyed by the real collar_id; re-key them to the row key.
        const rowKeyForCollar = new Map<string, string>();
        valid.forEach((h) => {
            const c = byId.get(keyOf(h)) ?? byId.get(`hole:${h.hole_id}`);
            if (c) rowKeyForCollar.set(c.collar_id, keyOf(h));
        });
        const rowSurveys = surveys
            .filter((s) => rowKeyForCollar.has(s.collar_id))
            .map((s) => ({ ...s, collar_id: rowKeyForCollar.get(s.collar_id) as string }));
        const deepest = new Map(valid.map((h) => [keyOf(h), Math.max(...h.bands.map((b) => b.to))]));
        const scene = buildScene3D(rows, rowSurveys, deepest, withBands.length - valid.length);

        const traces: Record<string, unknown>[] = [];

        valid.forEach((h) => {
            const key = keyOf(h);
            const top = scene.at(key, 0);
            if (!top) return;

            // One trace per hole, bands separated by null gaps.
            const xs: Array<number | null> = [];
            const ys: Array<number | null> = [];
            const zs: Array<number | null> = [];
            const colors: string[] = [];
            const text: string[] = [];

            h.bands.forEach((b) => {
                const seg = scene.segment(key, b.from, b.to);
                if (!seg) return;
                const isOre = b.code.endsWith('-ORE');
                const desc = `${h.hole_id} · ${b.from.toFixed(1)}–${b.to.toFixed(1)} m · ${b.code.replace('DERIVED-', '')}${isOre ? ' (U)' : ''}`;
                xs.push(...seg.x, null);
                ys.push(...seg.y, null);
                zs.push(...seg.z, null);
                for (let i = 0; i <= seg.x.length; i++) {
                    colors.push(b.color);
                    text.push(desc);
                }
            });

            traces.push({
                type: 'scatter3d',
                mode: 'lines',
                x: xs,
                y: ys,
                z: zs,
                line: { color: colors, width: 6 },
                connectgaps: false,
                text,
                hoverinfo: 'text',
                showlegend: false,
                name: h.hole_id,
            });

            const hasOre = h.bands.some((b) => b.code.endsWith('-ORE'));
            traces.push({
                type: 'scatter3d',
                mode: 'markers+text',
                x: [top.x],
                y: [top.y],
                z: [top.z],
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

        const layout: Record<string, unknown> = {
            scene: {
                xaxis: {
                    title: { text: EAST_AXIS_TITLE, font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    zerolinecolor: 'rgba(155,169,184,0.32)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                yaxis: {
                    title: { text: NORTH_AXIS_TITLE, font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    zerolinecolor: 'rgba(155,169,184,0.32)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                zaxis: {
                    title: { text: sceneZAxisTitle(scene), font: { color: '#9ba9b8', size: 10 } },
                    color: '#9ba9b8',
                    gridcolor: 'rgba(155,169,184,0.18)',
                    zerolinecolor: 'rgba(155,169,184,0.32)',
                    backgroundcolor: '#0a0e14',
                    showbackground: true,
                },
                bgcolor: '#0a0e14',
                // True scale on all three axes: a 1:1:0.6 box drew every dip
                // steeper or flatter than it is (FE-6 / GIS audit 2026-10). One
                // constant, so a vertical exaggeration is a one-line change.
                ...SCENE_3D_ASPECT,
                camera: { eye: { x: 1.6, y: 1.6, z: 0.8 }, up: { x: 0, y: 0, z: 1 } },
            },
            paper_bgcolor: '#0a0e14',
            plot_bgcolor: '#0a0e14',
            margin: { l: 0, r: 0, t: 0, b: 0 },
            showlegend: false,
            hovermode: 'closest',
        };

        return { data: traces, layout, caption: scene.caption };
    }, [holes, collars, surveys]);

    if (data.length === 0) {
        return (
            <div className="text-[11px] font-mono p-6 text-center" style={{ color: 'var(--fg-3)' }}>
                No 3D interval data — derive_intervals hasn't run for this project.
            </div>
        );
    }

    return (
        <div style={{ width: '100%', height }} className="flex flex-col">
            <div
                className="text-[10px] font-mono mb-1 shrink-0"
                style={{ color: 'var(--fg-3)' }}
                data-testid="desurvey-caption"
            >
                {caption}
            </div>
            <div className="flex-1 min-h-0">
                <GeoPlot data={data} layout={layout} />
            </div>
        </div>
    );
}

export default Borehole3DView;
