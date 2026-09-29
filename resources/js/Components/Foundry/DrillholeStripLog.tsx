import * as React from 'react';
import { useMemo, useState } from 'react';
import {
    AlterationTrack,
    IntervalDetail,
    LithologyTrack,
    MineralizationTrack,
    SwatchLegend,
    type TrackFrame,
} from '@/Components/Foundry/StripTracks';
import {
    alterationColourMap,
    lithologyLegend,
    mineralColourMap,
    type StripTracks,
} from '@/lib/stripLog';

/**
 * The hole page's strip log: a depth axis and one column per kind of logging.
 *
 *   LITHOLOGY       coloured by code (the data's hex colour, else a stable
 *                   legend colour), the code written in the band, the
 *                   description and attributes on hover and on click
 *   ALTERATION      one segment per alteration, side by side where two share
 *                   an interval, "type (intensity)" in the band
 *   MINERALIZATION  one band per mineral, "Pyrite 3%" where the data gave a
 *                   percentage, side by side where minerals overlap
 *   SAMPLES         the sampled windows, values on hover
 *
 * It replaces a single column that drew every kind of gold row on top of the
 * others with `color_hint` (which was colour TEXT or a rock code) as the fill.
 */

export interface SampleWindow {
    depth_from: number;
    depth_to: number;
    lithology_label?: string | null;
    assay_payload?: Record<string, unknown>;
}

const AXIS_W = 52;
const GAP = 8;
const TRACKS = { lithology: 150, alteration: 118, mineralization: 118, samples: 56 } as const;
const PAD_TOP = 26;
const PAD_BOTTOM = 10;

const STEPS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];

function tickStep(maxDepth: number): number {
    return STEPS.find((s) => maxDepth / s <= 12) ?? 1000;
}

function sampleLines(w: SampleWindow): string[] {
    const lines = [`Sample  ${w.depth_from}-${w.depth_to} m`];
    const payload = w.assay_payload ?? {};
    const keys = Object.keys(payload).slice(0, 8);
    for (const key of keys) {
        const value = payload[key];
        if (value !== null && typeof value !== 'object') lines.push(`${key}: ${String(value)}`);
    }
    return lines;
}

export default function DrillholeStripLog({
    tracks,
    sampleWindows = [],
    maxDepth,
}: {
    tracks: StripTracks;
    sampleWindows?: SampleWindow[];
    maxDepth: number;
}) {
    const [selected, setSelected] = useState<string[] | null>(null);

    const deepest = Math.max(
        maxDepth || 0,
        ...tracks.lithology.map((b) => b.to),
        ...tracks.alteration.map((b) => b.to),
        ...tracks.mineralization.map((b) => b.to),
        ...sampleWindows.map((w) => Number(w.depth_to)),
        1,
    );
    const height = Math.min(Math.max(deepest * 5, 320), 900);
    const usable = height;
    const yOf = (depth: number) => PAD_TOP + (depth / deepest) * usable;

    const layout = useMemo(() => {
        let x = AXIS_W;
        const place = (width: number): TrackFrame => {
            const frame = { x, width, yOf };
            x += width + GAP;
            return frame;
        };
        const lithology = place(TRACKS.lithology);
        const alteration = tracks.alteration.length ? place(TRACKS.alteration) : null;
        const mineralization = tracks.mineralization.length ? place(TRACKS.mineralization) : null;
        const samples = sampleWindows.length ? place(TRACKS.samples) : null;
        return { lithology, alteration, mineralization, samples, width: x };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tracks.alteration.length, tracks.mineralization.length, sampleWindows.length, deepest]);

    const step = tickStep(deepest);
    const ticks: number[] = [];
    for (let d = 0; d <= deepest; d += step) ticks.push(d);

    const lithLegend = lithologyLegend(tracks.lithology);
    const altTypes = Array.from(new Set(tracks.alteration.flatMap((b) => b.alterations.map((a) => a.type))));
    const minerals = Array.from(new Set(tracks.mineralization.map((b) => b.mineral)));
    const altColours = alterationColourMap(altTypes);
    const mineralColours = mineralColourMap(minerals);
    const truncated = tracks.truncated
        ? Object.entries(tracks.truncated).filter(([, cut]) => cut).map(([name]) => name)
        : [];

    return (
        <div>
            <div className="overflow-x-auto">
                <svg
                    width={layout.width}
                    height={height + PAD_TOP + PAD_BOTTOM}
                    viewBox={`0 0 ${layout.width} ${height + PAD_TOP + PAD_BOTTOM}`}
                    role="img"
                    aria-label="Strip log"
                    style={{ display: 'block' }}
                >
                    {/* Column headers */}
                    {[
                        ['LITHOLOGY', layout.lithology],
                        ['ALTERATION', layout.alteration],
                        ['MINERALIZATION', layout.mineralization],
                        ['SAMPLES', layout.samples],
                    ].map(([name, frame]) =>
                        frame ? (
                            <text
                                key={name as string}
                                x={(frame as TrackFrame).x + (frame as TrackFrame).width / 2}
                                y={16}
                                textAnchor="middle"
                                fontSize={9}
                                fontFamily="ui-monospace, monospace"
                                fill="var(--fg-3)"
                            >
                                {name as string}
                            </text>
                        ) : null,
                    )}

                    {/* Depth axis + grid */}
                    <text x={10} y={16} fontSize={9} fontFamily="ui-monospace, monospace" fill="var(--fg-3)">
                        DEPTH (m)
                    </text>
                    {ticks.map((d) => (
                        <g key={d}>
                            <line x1={AXIS_W - 4} y1={yOf(d)} x2={layout.width} y2={yOf(d)} stroke="var(--line-1)" strokeWidth={0.5} strokeDasharray="2 3" opacity={0.6} />
                            <text x={AXIS_W - 7} y={yOf(d) + 3} textAnchor="end" fontSize={9} fontFamily="ui-monospace, monospace" fill="var(--fg-3)">
                                {d}
                            </text>
                        </g>
                    ))}

                    {/* Column backgrounds */}
                    {[layout.lithology, layout.alteration, layout.mineralization, layout.samples].map((frame, i) =>
                        frame ? (
                            <rect key={i} x={frame.x} y={PAD_TOP} width={frame.width} height={usable} fill="var(--bg-2)" stroke="var(--line-1)" strokeWidth={0.5} />
                        ) : null,
                    )}

                    <LithologyTrack bands={tracks.lithology} frame={layout.lithology} onSelect={setSelected} />
                    {layout.alteration && (
                        <AlterationTrack bands={tracks.alteration} frame={layout.alteration} onSelect={setSelected} />
                    )}
                    {layout.mineralization && (
                        <MineralizationTrack bands={tracks.mineralization} frame={layout.mineralization} onSelect={setSelected} />
                    )}
                    {layout.samples &&
                        sampleWindows.map((w, i) => {
                            const y1 = yOf(Number(w.depth_from));
                            const h = Math.max(yOf(Number(w.depth_to)) - y1, 0.8);
                            const lines = sampleLines(w);
                            return (
                                <rect
                                    key={`s-${i}`}
                                    x={layout.samples!.x + 6}
                                    y={y1}
                                    width={layout.samples!.width - 12}
                                    height={h}
                                    fill="#64748b"
                                    stroke="rgba(0,0,0,0.25)"
                                    strokeWidth={0.4}
                                    style={{ cursor: 'pointer' }}
                                    role="listitem"
                                    aria-label={`Sample ${w.depth_from}-${w.depth_to} m`}
                                    onClick={() => setSelected(lines)}
                                >
                                    <title>{lines.join('\n')}</title>
                                </rect>
                            );
                        })}
                </svg>
            </div>

            <IntervalDetail lines={selected} onClose={() => setSelected(null)} />

            <div className="mt-3 flex flex-col gap-1.5">
                <SwatchLegend
                    title="Lithology"
                    entries={lithLegend.map((e) => ({ key: e.code, colour: e.colour, label: e.label ? `${e.code} · ${e.label.slice(0, 40)}` : e.code, hint: e.label }))}
                />
                <SwatchLegend
                    title="Alteration"
                    entries={altTypes.map((t) => ({ key: t, colour: altColours.get(t) ?? '#6b7280', label: t }))}
                />
                <SwatchLegend
                    title="Minerals"
                    entries={minerals.map((m) => ({ key: m, colour: mineralColours.get(m) ?? '#6b7280', label: m }))}
                />
                {truncated.length > 0 && (
                    <div className="text-[10px] font-mono" style={{ color: 'var(--warn, #d97706)' }} role="note">
                        Showing the first intervals only ({truncated.join(', ')}) - this hole has more than the strip log draws.
                    </div>
                )}
            </div>
        </div>
    );
}
