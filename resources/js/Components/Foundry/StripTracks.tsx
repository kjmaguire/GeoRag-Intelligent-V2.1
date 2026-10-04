import * as React from 'react';
import {
    alterationColourMap,
    alterationItemText,
    alterationLines,
    edgeOf,
    lithologyColourMap,
    lithologyLines,
    mineralColourMap,
    mineralLines,
    mineralText,
    packLanes,
    readableOn,
    type StripAlterationBand,
    type StripLithologyBand,
    type StripMineralBand,
} from '@/lib/stripLog';

/**
 * SVG track renderers for a strip log: lithology, alteration, mineralization.
 *
 * Each renders a `<g>` inside a caller-owned `<svg>`, into a column described by
 * a TrackFrame, so the same track draws in the hole page, the Workspace LOGS
 * column and any future surface. Every band carries a native `<title>` (hover)
 * and reports its lines to `onSelect` (click), which is how the description and
 * the attributes reach the geologist without a popover library.
 */

export interface TrackFrame {
    /** Left edge of the column, px. */
    x: number;
    width: number;
    /** Depth (m) to y (px). */
    yOf: (depth: number) => number;
}

interface SelectableProps {
    onSelect?: (lines: string[]) => void;
}

/** Most lanes drawn side by side; more overlap simply overlays the last lane. */
const MAX_LANES = 3;

const clipText = (text: string, room: number): string => {
    const chars = Math.max(0, Math.floor(room / 5.4));
    if (chars < 2) return '';
    return text.length <= chars ? text : `${text.slice(0, Math.max(1, chars - 1))}…`;
};

export function LithologyTrack({
    bands,
    frame,
    onSelect,
    selectedKey,
    codeLabel,
}: SelectableProps & {
    bands: StripLithologyBand[];
    frame: TrackFrame;
    selectedKey?: string | null;
    /** What to write in a band for a code (default: the code itself). */
    codeLabel?: (code: string) => string;
}) {
    // One colour per code on this hole: the data's hex colour, else a legend
    // colour that no other code of the hole shares.
    const colours = lithologyColourMap(bands);
    return (
        <g aria-label="Lithology">
            {bands.map((band, i) => {
                const y1 = frame.yOf(band.from);
                const y2 = frame.yOf(band.to);
                const h = Math.max(y2 - y1, 0.6);
                const fill = colours.get(band.code || '?') ?? '#6b7280';
                const lines = lithologyLines(band);
                const key = `lith-${i}`;
                const selected = selectedKey === key;
                return (
                    <g key={key}>
                        <rect
                            x={frame.x}
                            y={y1}
                            width={frame.width}
                            height={h}
                            fill={fill}
                            stroke={selected ? '#f59e0b' : edgeOf(fill)}
                            strokeWidth={selected ? 1.6 : 0.5}
                            style={{ cursor: onSelect ? 'pointer' : 'default' }}
                            role="listitem"
                            aria-label={`${band.code} ${band.from}-${band.to} m`}
                            onClick={onSelect ? () => onSelect(lines) : undefined}
                        >
                            <title>{lines.join('\n')}</title>
                        </rect>
                        {h >= 11 && frame.width >= 30 && (
                            <text
                                x={frame.x + 5}
                                y={y1 + h / 2 + 3.5}
                                fontSize={10}
                                fontFamily="ui-monospace, monospace"
                                fontWeight={600}
                                fill={readableOn(fill)}
                                style={{ pointerEvents: 'none', userSelect: 'none' }}
                            >
                                {clipText(codeLabel ? codeLabel(band.code) : band.code, frame.width - 8)}
                            </text>
                        )}
                    </g>
                );
            })}
        </g>
    );
}

export function AlterationTrack({
    bands,
    frame,
    onSelect,
}: SelectableProps & { bands: StripAlterationBand[]; frame: TrackFrame }) {
    const { placed, lanes } = packLanes(bands);
    const laneCount = Math.min(lanes, MAX_LANES);
    const laneW = frame.width / laneCount;
    const colours = alterationColourMap(Array.from(new Set(bands.flatMap((b) => b.alterations.map((a) => a.type)))));
    return (
        <g aria-label="Alteration">
            {placed.map(({ item: band, lane }, i) => {
                const y1 = frame.yOf(band.from);
                const h = Math.max(frame.yOf(band.to) - y1, 0.6);
                const laneX = frame.x + Math.min(lane, laneCount - 1) * laneW;
                const parts = band.alterations.length ? band.alterations : [null];
                const subW = laneW / parts.length;
                const lines = alterationLines(band);
                return (
                    <g
                        key={`alt-${i}`}
                        role="listitem"
                        aria-label={`Alteration ${band.label} ${band.from}-${band.to} m`}
                        style={{ cursor: onSelect ? 'pointer' : 'default' }}
                        onClick={onSelect ? () => onSelect(lines) : undefined}
                    >
                        {parts.map((item, j) => {
                            const fill = item ? (colours.get(item.type) ?? '#6b7280') : '#6b7280';
                            return (
                                <React.Fragment key={j}>
                                    <rect
                                        x={laneX + j * subW}
                                        y={y1}
                                        width={subW}
                                        height={h}
                                        fill={fill}
                                        stroke={edgeOf(fill)}
                                        strokeWidth={0.5}
                                    >
                                        <title>{lines.join('\n')}</title>
                                    </rect>
                                    {item && h >= 11 && subW >= 26 && (
                                        <text
                                            x={laneX + j * subW + 4}
                                            y={y1 + h / 2 + 3.5}
                                            fontSize={9}
                                            fontFamily="ui-monospace, monospace"
                                            fill={readableOn(fill)}
                                            style={{ pointerEvents: 'none', userSelect: 'none' }}
                                        >
                                            {clipText(alterationItemText(item), subW - 6)}
                                        </text>
                                    )}
                                </React.Fragment>
                            );
                        })}
                    </g>
                );
            })}
        </g>
    );
}

export function MineralizationTrack({
    bands,
    frame,
    onSelect,
}: SelectableProps & { bands: StripMineralBand[]; frame: TrackFrame }) {
    const { placed, lanes } = packLanes(bands);
    const laneCount = Math.min(lanes, MAX_LANES);
    const laneW = frame.width / laneCount;
    const colours = mineralColourMap(Array.from(new Set(bands.map((b) => b.mineral))));
    return (
        <g aria-label="Mineralization">
            {placed.map(({ item: band, lane }, i) => {
                const y1 = frame.yOf(band.from);
                const h = Math.max(frame.yOf(band.to) - y1, 0.6);
                const fill = colours.get(band.mineral) ?? '#6b7280';
                const lines = mineralLines(band);
                const x = frame.x + Math.min(lane, laneCount - 1) * laneW;
                return (
                    <g key={`min-${i}`}>
                        <rect
                            x={x}
                            y={y1}
                            width={laneW}
                            height={h}
                            fill={fill}
                            stroke={edgeOf(fill)}
                            strokeWidth={0.5}
                            style={{ cursor: onSelect ? 'pointer' : 'default' }}
                            role="listitem"
                            aria-label={`${mineralText(band)} ${band.from}-${band.to} m`}
                            onClick={onSelect ? () => onSelect(lines) : undefined}
                        >
                            <title>{lines.join('\n')}</title>
                        </rect>
                        {h >= 10 && laneW >= 26 && (
                            <text
                                x={x + 4}
                                y={y1 + h / 2 + 3.5}
                                fontSize={9}
                                fontFamily="ui-monospace, monospace"
                                fill={readableOn(fill)}
                                style={{ pointerEvents: 'none', userSelect: 'none' }}
                            >
                                {clipText(mineralText(band), laneW - 6)}
                            </text>
                        )}
                    </g>
                );
            })}
        </g>
    );
}

/** A row of swatches: what each colour on a track means. */
export function SwatchLegend({
    title,
    entries,
}: {
    title: string;
    entries: { key: string; colour: string; label: string; hint?: string }[];
}) {
    if (entries.length === 0) return null;
    return (
        <div
            className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] font-mono"
            aria-label={`${title} legend`}
        >
            <span className="uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                {title}
            </span>
            {entries.map((e) => (
                <span
                    key={e.key}
                    className="flex items-center gap-1.5"
                    style={{ color: 'var(--fg-2)' }}
                    title={e.hint || e.label}
                >
                    <span
                        style={{
                            display: 'inline-block',
                            width: 10,
                            height: 10,
                            background: e.colour,
                            border: '1px solid rgba(0,0,0,0.25)',
                        }}
                    />
                    <span>{e.label}</span>
                </span>
            ))}
        </div>
    );
}

/** The clicked band's description and attributes. */
export function IntervalDetail({ lines, onClose }: { lines: string[] | null; onClose?: () => void }) {
    if (!lines || lines.length === 0) return null;
    const [head, ...rest] = lines;
    return (
        <div
            className="text-[11px] font-mono rounded border px-3 py-2 mt-2"
            style={{ borderColor: 'var(--line-1)', background: 'var(--bg-2)', color: 'var(--fg-1)' }}
            role="status"
            aria-label="Selected interval"
        >
            <div className="flex items-start justify-between gap-2">
                <div style={{ color: 'var(--fg-0)', fontWeight: 600 }}>{head}</div>
                {onClose && (
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-[10px]"
                        style={{ color: 'var(--fg-3)' }}
                        aria-label="Clear selection"
                    >
                        clear
                    </button>
                )}
            </div>
            {rest.map((line, i) => (
                <div key={i} style={{ color: 'var(--fg-2)', whiteSpace: 'pre-wrap' }}>
                    {line}
                </div>
            ))}
        </div>
    );
}
