import * as React from 'react';
import { useState } from 'react';
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
    lithologyColourMap,
    mineralColourMap,
    type LithologyDetail,
    type StripAlterationBand,
    type StripMineralBand,
} from '@/lib/stripLog';

/**
 * Foundry chart primitives — pure SVG, no external deps: stereonet, rose,
 * downhole multi-log, lithology strip column and chrono column. Real data is
 * passed in via props.
 *
 * `Components/HoleAnalysis/*` covers the Plotly-based GeoCharts.
 */

/* ============================================================
   StereonetMini — schematic equal-area stereonet (poles only)
   ============================================================ */

/** A line in space: trend clockwise from north, plunge down from horizontal. */
export interface StereonetPole {
    trend_deg: number;
    plunge_deg: number;
}

interface StereonetMiniProps {
    /**
     * POLES, not plane attitudes.
     *
     * The prop used to be `measurements: {dip_direction, dip}` while the
     * projection below did `sin((90 - dip)/2)` — the equal-area formula for
     * a LINE at that plunge. Feeding it a plane's dip would have put a
     * horizontal bed on the primitive circle, which is where the pole to a
     * VERTICAL plane belongs: every point ninety degrees out of position.
     *
     * It never rendered a wrong plot only because its one caller passed
     * `[]`. Naming it `poles` is what stops the next caller from finding
     * out the hard way. Convert with the same rule `Stereonet.poleOfPlane`
     * uses: trend = (dip_direction + 180) mod 360, plunge = 90 - dip.
     */
    poles?: StereonetPole[];
    size?: number;
}

export function StereonetMini({ poles, size = 200 }: StereonetMiniProps) {
    const data = poles ?? [];
    const r = size / 2 - 6;
    const cx = size / 2;
    const cy = size / 2;
    return (
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
            <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--line-2)" strokeWidth="1" />
            <circle
                cx={cx}
                cy={cy}
                r={r / 2}
                fill="none"
                stroke="var(--line-1)"
                strokeWidth="0.5"
                strokeDasharray="2 2"
            />
            <line x1={cx - r} x2={cx + r} y1={cy} y2={cy} stroke="var(--line-1)" strokeWidth="0.5" />
            <line x1={cx} x2={cx} y1={cy - r} y2={cy + r} stroke="var(--line-1)" strokeWidth="0.5" />
            {data.map((p, i) => {
                // Equal-area (Schmidt), lower hemisphere:
                //     r / R = √2 · sin((90° − plunge) / 2)
                // North up, east right; SVG y grows downward, hence −cos.
                const plungeRad = (p.plunge_deg * Math.PI) / 180;
                const trendRad = (p.trend_deg * Math.PI) / 180;
                const rho = r * Math.sin((Math.PI / 2 - plungeRad) / 2) * Math.SQRT2;
                const x = cx + rho * Math.sin(trendRad);
                const y = cy - rho * Math.cos(trendRad);
                return <circle key={i} cx={x} cy={y} r={2.2} fill="var(--accent)" opacity={0.85} />;
            })}
            <text
                x={cx}
                y={cy - r - 2}
                fill="var(--fg-3)"
                fontSize="9"
                textAnchor="middle"
                fontFamily="var(--font-mono)"
            >
                N
            </text>
        </svg>
    );
}

/* ============================================================
   RoseMini — strike frequency rose diagram
   ============================================================ */

export function RoseMini({ strikes, size = 200 }: { strikes?: number[]; size?: number }) {
    const data = strikes ?? [];
    const bins = 36;
    const counts = new Array(bins).fill(0);
    data.forEach((s) => {
        const idx = Math.floor(((s % 360) / 360) * bins);
        counts[idx]++;
    });
    const max = Math.max(1, ...counts);
    const cx = size / 2;
    const cy = size / 2;
    const r = size / 2 - 6;

    return (
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
            <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--line-2)" strokeWidth="1" />
            {counts.map((c, i) => {
                if (c === 0) return null;
                const angle1 = (i / bins) * 2 * Math.PI - Math.PI / 2;
                const angle2 = ((i + 1) / bins) * 2 * Math.PI - Math.PI / 2;
                const len = (c / max) * r;
                const x1 = cx + len * Math.cos(angle1);
                const y1 = cy + len * Math.sin(angle1);
                const x2 = cx + len * Math.cos(angle2);
                const y2 = cy + len * Math.sin(angle2);
                return (
                    <path
                        key={i}
                        d={`M${cx},${cy} L${x1},${y1} A${len},${len} 0 0 1 ${x2},${y2} Z`}
                        fill="var(--accent)"
                        opacity={0.55}
                        stroke="var(--accent-dim)"
                        strokeWidth="0.4"
                    />
                );
            })}
            <text
                x={cx}
                y={cy - r - 2}
                fill="var(--fg-3)"
                fontSize="9"
                textAnchor="middle"
                fontFamily="var(--font-mono)"
            >
                N
            </text>
        </svg>
    );
}

/* ============================================================
   DownholeMultiLog — gamma / resistivity / density tracks
   ============================================================ */

interface DownholeTrack {
    label: string;
    color: string;
    points: Array<{ depth: number; value: number }>;
    min: number;
    max: number;
}

export function DownholeMultiLog({
    tracks,
    depthMax = 600,
    height = 360,
    trackWidth = 80,
}: {
    tracks?: DownholeTrack[];
    depthMax?: number;
    height?: number;
    trackWidth?: number;
}) {
    const data = tracks ?? [];
    if (data.length === 0) {
        return (
            <div className="text-[11px] font-mono p-4 text-center" style={{ color: 'var(--fg-3)' }}>
                No log curves loaded.
            </div>
        );
    }
    const width = data.length * (trackWidth + 6) + 40;
    return (
        <svg width={width} height={height + 20} viewBox={`0 0 ${width} ${height + 20}`}>
            {/* Depth axis */}
            <text x={4} y={12} fill="var(--fg-3)" fontSize="9" fontFamily="var(--font-mono)">
                DEPTH (m)
            </text>
            {[0, 0.25, 0.5, 0.75, 1].map((p) => (
                <g key={p}>
                    <line
                        x1={36}
                        y1={p * height + 16}
                        x2={width}
                        y2={p * height + 16}
                        stroke="var(--line-1)"
                        strokeDasharray="2 2"
                        strokeWidth="0.4"
                    />
                    <text x={4} y={p * height + 20} fill="var(--fg-3)" fontSize="9" fontFamily="var(--font-mono)">
                        {Math.round(p * depthMax)}
                    </text>
                </g>
            ))}
            {data.map((t, i) => {
                const x0 = 40 + i * (trackWidth + 6);
                const range = t.max - t.min || 1;
                const d = t.points
                    .map((p, j) => {
                        const x = x0 + ((p.value - t.min) / range) * trackWidth;
                        const y = 16 + (p.depth / depthMax) * height;
                        return `${j === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
                    })
                    .join(' ');
                return (
                    <g key={i}>
                        <rect
                            x={x0}
                            y={16}
                            width={trackWidth}
                            height={height}
                            fill="var(--bg-2)"
                            stroke="var(--line-1)"
                            strokeWidth="0.5"
                        />
                        <text
                            x={x0 + trackWidth / 2}
                            y={12}
                            fill="var(--fg-2)"
                            fontSize="9"
                            textAnchor="middle"
                            fontFamily="var(--font-mono)"
                        >
                            {t.label}
                        </text>
                        <path d={d} fill="none" stroke={t.color} strokeWidth="1.2" />
                    </g>
                );
            })}
        </svg>
    );
}

/* ============================================================
   LithologyStripColumn — hole-specific derived lithology bands
   ============================================================ */

export interface LithologyInterval {
    from: number;
    to: number;
    code: string;
    label: string;
    /** A hex display colour, or '' - the strip log assigns a legend colour per code. */
    color: string;
    /** Description, colour as described, grain size, hardness, weathering, RQD, recovery. */
    detail?: LithologyDetail;
}

/**
 * Round a max-depth value UP to the nearest friendly tick so the depth
 * axis never visually cuts off the bottom band and never leaves huge
 * dead space below it.
 *   < 100 m  → nearest 25 m
 *   < 300 m  → nearest 50 m
 *   else     → nearest 100 m
 * The small additive constant guarantees we always round to the NEXT
 * tick even when the data lands exactly on a tick.
 */
function roundUpDepth(d: number): number {
    if (d <= 0) return 50;
    if (d < 100) return Math.ceil((d + 5) / 25) * 25;
    if (d < 300) return Math.ceil((d + 10) / 50) * 50;
    return Math.ceil((d + 15) / 100) * 100;
}

const LITHO_SHORT: Record<string, string> = {
    'DERIVED-ORE': 'ORE',
    'DERIVED-SST': 'SST',
    'DERIVED-SHALE': 'SHL',
    'DERIVED-MIX': 'MIX',
    'DERIVED-SURF': 'SURF',
};

export function LithologyStripColumn({
    intervals,
    holeId,
    height = 520,
    width = 220,
    alteration = [],
    mineralization = [],
    truncated,
}: {
    intervals: LithologyInterval[];
    holeId: string | null;
    depthMax: number;
    height?: number;
    width?: number;
    alteration?: StripAlterationBand[];
    mineralization?: StripMineralBand[];
    /** Tracks the server cut at its per-track bound. */
    truncated?: { lithology?: boolean; alteration?: boolean; mineralization?: boolean };
}) {
    const [selected, setSelected] = useState<string[] | null>(null);
    const hasOther = alteration.length > 0 || mineralization.length > 0;
    if (!intervals.length && !hasOther) {
        return (
            <div
                className="text-[11px] font-mono p-4 text-center"
                style={{
                    color: 'var(--fg-3)',
                    background: 'var(--bg-1)',
                    border: '1px solid var(--line-1)',
                    borderRadius: 6,
                }}
            >
                No lithology logged for this hole.
            </div>
        );
    }
    const padT = 32;
    const padB = 14;
    const usableH = height - padT - padB;
    // Fit the depth axis to this hole's actual data + a small buffer,
    // rounded up to a friendly tick. Without this a 130 m hole on a
    // 300 m global axis leaves half the column visually empty.
    const dataMax = Math.max(
        1,
        ...intervals.map((i) => i.to),
        ...alteration.map((b) => b.to),
        ...mineralization.map((b) => b.to),
    );
    const denom = roundUpDepth(dataMax);
    const yOf = (d: number) => padT + (d / denom) * usableH;
    const derived = intervals.some((i) => i.code.startsWith('DERIVED-'));
    const oreCount = intervals.filter((i) => i.code.endsWith('-ORE')).length;

    // Layout: left depth axis (40px) + one column per kind of logging. The
    // lithology column keeps the larger share; alteration and mineralization
    // appear only when the hole has them.
    const axisW = 40;
    const bandX = axisW + 4;
    const totalW = width - bandX - 6;
    const weights = [
        { key: 'lith', w: 2 },
        ...(alteration.length ? [{ key: 'alt', w: 1.4 }] : []),
        ...(mineralization.length ? [{ key: 'min', w: 1.4 }] : []),
    ];
    const gap = 4;
    const usable = totalW - gap * (weights.length - 1);
    const weightSum = weights.reduce((a, c) => a + c.w, 0);
    const frames: Record<string, TrackFrame> = {};
    let cursor = bandX;
    for (const { key, w } of weights) {
        const colW = (usable * w) / weightSum;
        frames[key] = { x: cursor, width: colW, yOf };
        cursor += colW + gap;
    }

    // Grid lines every 25m up to denom, capped at 12 lines to avoid clutter.
    const gridStepM = denom > 600 ? 100 : denom > 300 ? 50 : 25;
    const gridLines: number[] = [];
    for (let d = gridStepM; d < denom; d += gridStepM) {
        gridLines.push(d);
    }

    // Build a unique legend of lithology codes seen in this hole, in the colour
    // each is drawn in (the data's hex colour, else the code's legend colour).
    const legendCodes = Array.from(new Set(intervals.map((i) => i.code)));
    const legendColors = lithologyColourMap(intervals);
    const altTypes = Array.from(new Set(alteration.flatMap((b) => b.alterations.map((a) => a.type))));
    const minerals = Array.from(new Set(mineralization.map((b) => b.mineral)));
    const altColours = alterationColourMap(altTypes);
    const mineralColours = mineralColourMap(minerals);
    const cut = Object.entries(truncated ?? {})
        .filter(([, v]) => v)
        .map(([k]) => k);

    return (
        <div
            style={{
                background: 'var(--bg-1)',
                border: '1px solid var(--line-1)',
                borderRadius: 6,
                padding: 10,
                width: width + 20,
            }}
        >
            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                {derived ? 'Lithology · derived' : 'Lithology'}
            </div>
            <div className="text-[10px] font-mono" style={{ color: 'var(--fg-2)' }}>
                {holeId ?? '—'} · {intervals.length} bands
                {oreCount > 0 ? ` · ${oreCount} U-host` : ''}
                {alteration.length > 0 ? ` · ${alteration.length} alteration` : ''}
                {mineralization.length > 0 ? ` · ${mineralization.length} mineral` : ''}
            </div>
            {/* Legend — right under the header, before the user's eye reaches the bars. */}
            <div
                className="mt-1.5 mb-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] font-mono"
                style={{ color: 'var(--fg-2)' }}
            >
                {legendCodes.map((code) => {
                    const short = LITHO_SHORT[code] ?? code.replace('DERIVED-', '');
                    const isOre = code.endsWith('-ORE');
                    return (
                        <span key={code} className="flex items-center gap-1.5">
                            <span
                                style={{
                                    display: 'inline-block',
                                    width: 10,
                                    height: 10,
                                    background: legendColors.get(code),
                                    border: '1px solid rgba(0,0,0,0.25)',
                                }}
                            />
                            <span style={{ color: isOre ? '#8fe28b' : 'var(--fg-2)', fontWeight: isOre ? 600 : 400 }}>
                                {short}
                            </span>
                        </span>
                    );
                })}
            </div>
            <svg
                width={width}
                height={height}
                viewBox={`0 0 ${width} ${height}`}
                style={{ display: 'block' }}
                role="img"
                aria-label="Lithology strip log"
            >
                {/* Header */}
                <text
                    x={axisW / 2}
                    y={padT - 8}
                    textAnchor="middle"
                    fontSize="9"
                    fill="var(--fg-3)"
                    fontFamily="var(--font-mono)"
                >
                    DEPTH (m)
                </text>
                <text
                    x={frames.lith.x + frames.lith.width / 2}
                    y={padT - 8}
                    textAnchor="middle"
                    fontSize="9"
                    fill="var(--fg-3)"
                    fontFamily="var(--font-mono)"
                >
                    LITHOLOGY
                </text>
                {frames.alt && (
                    <text
                        x={frames.alt.x + frames.alt.width / 2}
                        y={padT - 8}
                        textAnchor="middle"
                        fontSize="9"
                        fill="var(--fg-3)"
                        fontFamily="var(--font-mono)"
                    >
                        ALTERATION
                    </text>
                )}
                {frames.min && (
                    <text
                        x={frames.min.x + frames.min.width / 2}
                        y={padT - 8}
                        textAnchor="middle"
                        fontSize="9"
                        fill="var(--fg-3)"
                        fontFamily="var(--font-mono)"
                    >
                        MINERALS
                    </text>
                )}

                {/* Depth grid lines across the band area */}
                {gridLines.map((d) => {
                    const y = yOf(d);
                    return (
                        <g key={`grid-${d}`}>
                            <line
                                x1={axisW - 2}
                                y1={y}
                                x2={width}
                                y2={y}
                                stroke="var(--line-1)"
                                strokeWidth="0.5"
                                strokeDasharray="2 3"
                                opacity="0.5"
                            />
                            <text
                                x={axisW - 4}
                                y={y + 3}
                                textAnchor="end"
                                fontSize="9"
                                fill="var(--fg-3)"
                                fontFamily="var(--font-mono)"
                            >
                                {d}
                            </text>
                        </g>
                    );
                })}

                {/* Lithology bands: coloured by code, code in the band, description on hover and click */}
                <LithologyTrack
                    bands={intervals}
                    frame={frames.lith}
                    onSelect={setSelected}
                    // A gamma-derived band shows its short form (ORE, SST ...).
                    codeLabel={(code) => LITHO_SHORT[code] ?? code.replace('DERIVED-', '')}
                />
                {frames.alt && <AlterationTrack bands={alteration} frame={frames.alt} onSelect={setSelected} />}
                {frames.min && <MineralizationTrack bands={mineralization} frame={frames.min} onSelect={setSelected} />}
            </svg>
            <IntervalDetail lines={selected} onClose={() => setSelected(null)} />
            {(altTypes.length > 0 || minerals.length > 0) && (
                <div className="mt-2 flex flex-col gap-1">
                    <SwatchLegend
                        title="Alteration"
                        entries={altTypes.map((t) => ({ key: t, colour: altColours.get(t) ?? '#6b7280', label: t }))}
                    />
                    <SwatchLegend
                        title="Minerals"
                        entries={minerals.map((m) => ({
                            key: m,
                            colour: mineralColours.get(m) ?? '#6b7280',
                            label: m,
                        }))}
                    />
                </div>
            )}
            {cut.length > 0 && (
                <div className="mt-1 text-[10px] font-mono" style={{ color: 'var(--warn, #d97706)' }} role="note">
                    Showing the first intervals only ({cut.join(', ')}).
                </div>
            )}
        </div>
    );
}

/* ============================================================
   ChronoColumn — chronostratigraphic / age column
   ============================================================ */

export interface StratUnit {
    age: string;
    age_period?: string;
    unit_name: string;
    color: string;
    lithology?: string | null;
    is_host?: boolean;
    is_unconformity?: boolean;
    notes?: string[];
}

export function ChronoColumn({
    units,
    height = 540,
    title = 'Stratigraphic column',
    eyebrow,
    width = 360,
}: {
    units: StratUnit[];
    height?: number;
    title?: string;
    eyebrow?: string;
    width?: number;
}) {
    if (!units.length) {
        return (
            <div className="text-[11px] font-mono p-4 text-center" style={{ color: 'var(--fg-3)' }}>
                No stratigraphic units loaded.
            </div>
        );
    }
    const padT = 20;
    const padB = 12;
    const usableH = height - padT - padB;
    // Equal-thickness slots; could weight by age-span later.
    const slotH = usableH / units.length;

    return (
        <div style={{ background: 'var(--bg-1)', border: '1px solid var(--line-1)', borderRadius: 6, padding: 12 }}>
            {eyebrow && (
                <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                    {eyebrow}
                </div>
            )}
            <div className="text-xs font-medium mb-2" style={{ color: 'var(--fg-0)' }}>
                {title}
            </div>
            <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{ display: 'block' }}>
                <g style={{ fontFamily: 'var(--font-mono)', fontSize: 9 }}>
                    <text x={42} y={padT - 6} textAnchor="middle" fill="var(--fg-3)">
                        AGE
                    </text>
                    <text x={width / 2 + 20} y={padT - 6} textAnchor="middle" fill="var(--fg-3)">
                        UNIT
                    </text>
                </g>
                {units.map((u, i) => {
                    const y = padT + i * slotH;
                    return (
                        <g key={i}>
                            <rect
                                x={8}
                                y={y}
                                width={74}
                                height={slotH}
                                fill={u.color}
                                fillOpacity={u.is_unconformity ? 0.35 : 0.7}
                                stroke="var(--bg-0)"
                                strokeWidth="0.6"
                                strokeDasharray={u.is_unconformity ? '4 4' : '0'}
                            />
                            <text
                                x={45}
                                y={y + slotH / 2 + 3}
                                textAnchor="middle"
                                fontSize="9.5"
                                fill="oklch(0.18 0.04 50)"
                                fontFamily="var(--font-mono)"
                                fontWeight="500"
                            >
                                {u.age}
                            </text>
                            <text
                                x={92}
                                y={y + 14}
                                fontSize="11"
                                fill="var(--fg-0)"
                                fontWeight={u.is_host ? '600' : '500'}
                            >
                                {u.unit_name}
                            </text>
                            {u.age_period && (
                                <text x={92} y={y + 26} fontSize="9" fill="var(--fg-3)" fontFamily="var(--font-mono)">
                                    {u.age_period}
                                    {u.lithology ? ` · ${u.lithology}` : ''}
                                </text>
                            )}
                            {(u.notes ?? []).slice(0, 2).map((n, j) => (
                                <text key={j} x={92} y={y + 40 + j * 12} fontSize="9.5" fill="var(--fg-2)">
                                    · {n}
                                </text>
                            ))}
                            {u.is_host && (
                                <>
                                    <circle
                                        cx={width - 16}
                                        cy={y + slotH / 2}
                                        r="5"
                                        fill="oklch(0.82 0.18 145)"
                                        stroke="var(--bg-0)"
                                        strokeWidth="1.2"
                                    />
                                    <text
                                        x={width - 16}
                                        y={y + slotH / 2 + 16}
                                        textAnchor="middle"
                                        fontSize="8.5"
                                        fontFamily="var(--font-mono)"
                                        fill="oklch(0.82 0.18 145)"
                                    >
                                        U HOST
                                    </text>
                                </>
                            )}
                            {i < units.length - 1 && (
                                <line
                                    x1={8}
                                    y1={y + slotH}
                                    x2={width - 8}
                                    y2={y + slotH}
                                    stroke="var(--line-2)"
                                    strokeWidth="0.6"
                                />
                            )}
                        </g>
                    );
                })}
            </svg>
        </div>
    );
}
