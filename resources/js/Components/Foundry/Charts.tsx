import * as React from 'react';

/**
 * Foundry chart primitives — pure SVG, no external deps. Ports the prototype's
 * `analytics/DemoCharts.jsx` (5 new analytics charts) + the most-used
 * `shared/GeoCharts.jsx` shapes (stereonet/rose/downhole/ternary skeletons).
 *
 * Existing `Components/HoleAnalysis/*` covers production GeoCharts with Plotly;
 * these are foundry-styled overlays for the AuditLog, Portfolio, and
 * ProjectAnalytics surfaces. Real data is passed in via props; the prototype's
 * synthetic constants are kept ONLY as defaults so the surface still renders if
 * the data prop is missing.
 */

/* ============================================================
   RefusalByGate — stacked bar by hallucination gate, by week
   ============================================================ */

interface RefusalByGateProps {
    weeks?: Array<{ week: string; gates: Record<string, number> }>;
}

const DEFAULT_REFUSAL_GATES = [
    { id: 'g6_calibration', label: 'g6 confidence floor', color: 'oklch(0.72 0.18 25)' },
    { id: 'g4_citation_anchor', label: 'g4 citation anchor', color: 'oklch(0.78 0.15 75)' },
    { id: 'g2_route_oos', label: 'g2 routing OOS', color: 'oklch(0.78 0.14 230)' },
    { id: 'g5_typed_output', label: 'g5 typed output', color: 'oklch(0.74 0.16 280)' },
    { id: 'g1_safety', label: 'g1 safety', color: 'oklch(0.62 0.04 240)' },
];

export function RefusalByGate({ weeks }: RefusalByGateProps) {
    const data = weeks && weeks.length > 0
        ? weeks
        : Array.from({ length: 12 }).map((_, i) => ({
            week: `W${i + 1}`,
            gates: DEFAULT_REFUSAL_GATES.reduce<Record<string, number>>((acc, g) => {
                acc[g.id] = 0;
                return acc;
            }, {}),
        }));

    const maxTotal = Math.max(1, ...data.map((d) => Object.values(d.gates).reduce((a, b) => a + b, 0)));
    const barWidth = 20;
    const gap = 6;
    const height = 140;
    const width = data.length * (barWidth + gap);

    return (
        <div>
            <svg width="100%" height={height + 30} viewBox={`0 0 ${width} ${height + 30}`} preserveAspectRatio="none">
                {data.map((d, i) => {
                    let yCursor = height;
                    return (
                        <g key={i} transform={`translate(${i * (barWidth + gap)}, 0)`}>
                            {DEFAULT_REFUSAL_GATES.map((g) => {
                                const v = d.gates[g.id] ?? 0;
                                const h = (v / maxTotal) * height;
                                yCursor -= h;
                                return <rect key={g.id} x={0} y={yCursor} width={barWidth} height={h} fill={g.color} />;
                            })}
                            <text x={barWidth / 2} y={height + 14} fill="var(--fg-3)" fontSize="9" textAnchor="middle" fontFamily="var(--font-mono)">
                                {d.week}
                            </text>
                        </g>
                    );
                })}
            </svg>
            <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2">
                {DEFAULT_REFUSAL_GATES.map((g) => (
                    <span key={g.id} className="inline-flex items-center gap-1.5 text-[10px] font-mono" style={{ color: 'var(--fg-2)' }}>
                        <span className="w-2 h-2 rounded-sm inline-block" style={{ background: g.color }} />
                        {g.label}
                    </span>
                ))}
            </div>
        </div>
    );
}

/* ============================================================
   ConfidenceHistogram — distribution with refusal-floor line
   ============================================================ */

interface ConfidenceHistogramProps {
    bins?: Array<{ low: number; high: number; count: number }>;
    refusalFloor?: number;
}

export function ConfidenceHistogram({ bins, refusalFloor = 0.5 }: ConfidenceHistogramProps) {
    const data = bins && bins.length > 0
        ? bins
        : Array.from({ length: 20 }).map((_, i) => ({ low: i * 0.05, high: (i + 1) * 0.05, count: 0 }));
    const maxCount = Math.max(1, ...data.map((b) => b.count));
    const width = 320;
    const height = 120;
    const binWidth = width / data.length;
    const floorX = refusalFloor * width;

    return (
        <svg width="100%" height={height + 20} viewBox={`0 0 ${width} ${height + 20}`} preserveAspectRatio="none">
            {data.map((b, i) => {
                const h = (b.count / maxCount) * height;
                const above = b.high >= refusalFloor;
                return (
                    <rect
                        key={i}
                        x={i * binWidth + 1}
                        y={height - h}
                        width={binWidth - 2}
                        height={h}
                        fill={above ? 'var(--accent)' : 'var(--fg-3)'}
                        opacity={above ? 0.85 : 0.4}
                    />
                );
            })}
            <line x1={floorX} x2={floorX} y1={0} y2={height} stroke="var(--warn)" strokeDasharray="4 2" strokeWidth="1.4" />
            <text x={floorX + 4} y={12} fill="var(--warn)" fontSize="9" fontFamily="var(--font-mono)">
                refusal floor · {refusalFloor.toFixed(2)}
            </text>
            <text x={0} y={height + 14} fill="var(--fg-3)" fontSize="9" fontFamily="var(--font-mono)">
                0.0
            </text>
            <text x={width} y={height + 14} fill="var(--fg-3)" fontSize="9" textAnchor="end" fontFamily="var(--font-mono)">
                1.0
            </text>
        </svg>
    );
}

/* ============================================================
   InvestigationFunnel — created → cited
   ============================================================ */

interface InvestigationFunnelProps {
    stages?: Array<{ label: string; count: number }>;
}

const DEFAULT_FUNNEL = [
    { label: 'Created', count: 0 },
    { label: 'Pinned', count: 0 },
    { label: 'Reviewed', count: 0 },
    { label: 'Published', count: 0 },
    { label: 'Cited', count: 0 },
];

export function InvestigationFunnel({ stages }: InvestigationFunnelProps) {
    const data = stages && stages.length > 0 ? stages : DEFAULT_FUNNEL;
    const max = Math.max(1, ...data.map((s) => s.count));
    return (
        <div className="flex flex-col gap-2">
            {data.map((s, i) => {
                const pct = (s.count / max) * 100;
                return (
                    <div key={i}>
                        <div className="flex justify-between text-[10px] font-mono uppercase tracking-wider mb-0.5" style={{ color: 'var(--fg-3)' }}>
                            <span>{s.label}</span>
                            <span>{s.count}</span>
                        </div>
                        <div className="h-3 rounded-sm overflow-hidden" style={{ background: 'var(--bg-3)' }}>
                            <div
                                style={{
                                    width: `${Math.max(2, pct)}%`,
                                    height: '100%',
                                    background: `oklch(0.62 0.12 ${160 - i * 14})`,
                                    transition: 'width 0.5s ease',
                                }}
                            />
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

/* ============================================================
   QueryDensityHeatmap — toggleable map overlay (SVG stand-in)
   ============================================================ */

interface QueryDensityHeatmapProps {
    cells?: Array<{ x: number; y: number; intensity: number }>;
    width?: number;
    height?: number;
}

export function QueryDensityHeatmap({ cells, width = 320, height = 200 }: QueryDensityHeatmapProps) {
    const data = cells && cells.length > 0 ? cells : [];
    const max = Math.max(0.0001, ...data.map((c) => c.intensity));

    return (
        <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
            <rect width={width} height={height} fill="var(--bg-2)" />
            {data.map((c, i) => {
                const opacity = Math.min(1, (c.intensity / max) * 1.2);
                return (
                    <circle
                        key={i}
                        cx={c.x * width}
                        cy={c.y * height}
                        r={16}
                        fill="var(--accent)"
                        opacity={opacity * 0.6}
                        style={{ mixBlendMode: 'screen' }}
                    />
                );
            })}
            {data.length === 0 && (
                <text x={width / 2} y={height / 2} fill="var(--fg-3)" fontSize="11" textAnchor="middle" fontFamily="var(--font-mono)">
                    No query density data
                </text>
            )}
        </svg>
    );
}

export function HeatmapLegend({ max = 100 }: { max?: number }) {
    return (
        <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
            <span>0</span>
            <div className="w-32 h-2 rounded-sm" style={{ background: 'linear-gradient(90deg, transparent, var(--accent))' }} />
            <span>{max} queries</span>
        </div>
    );
}

/* ============================================================
   PerJurisdictionVolume — small horizontal bars
   ============================================================ */

interface PerJurisdictionVolumeProps {
    rows?: Array<{ code: string; name: string; volume: number; tier3?: boolean }>;
}

export function PerJurisdictionVolume({ rows }: PerJurisdictionVolumeProps) {
    const data = rows && rows.length > 0 ? rows : [];
    const max = Math.max(1, ...data.map((r) => r.volume));
    if (data.length === 0) {
        return <div className="text-[11px] font-mono" style={{ color: 'var(--fg-3)' }}>No jurisdiction volume data.</div>;
    }
    return (
        <div className="flex flex-col gap-1.5">
            {data.map((r) => (
                <div key={r.code} className="grid grid-cols-[40px_1fr_60px] gap-2 items-center text-xs">
                    <span className="font-mono" style={{ color: 'var(--fg-2)' }}>{r.code}</span>
                    <div className="h-2 rounded-sm" style={{ background: 'var(--bg-3)' }}>
                        <div
                            style={{
                                width: `${(r.volume / max) * 100}%`,
                                height: '100%',
                                background: r.tier3 ? 'var(--warn)' : 'var(--accent)',
                            }}
                        />
                    </div>
                    <span className="font-mono text-right" style={{ color: 'var(--fg-1)' }}>{r.volume}</span>
                </div>
            ))}
        </div>
    );
}

/* ============================================================
   StereonetMini — schematic equal-area stereonet (poles only)
   ============================================================ */

interface StereonetMiniProps {
    measurements?: Array<{ dip_direction: number; dip: number }>;
    size?: number;
}

export function StereonetMini({ measurements, size = 200 }: StereonetMiniProps) {
    const data = measurements ?? [];
    const r = size / 2 - 6;
    const cx = size / 2;
    const cy = size / 2;
    return (
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
            <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--line-2)" strokeWidth="1" />
            <circle cx={cx} cy={cy} r={r / 2} fill="none" stroke="var(--line-1)" strokeWidth="0.5" strokeDasharray="2 2" />
            <line x1={cx - r} x2={cx + r} y1={cy} y2={cy} stroke="var(--line-1)" strokeWidth="0.5" />
            <line x1={cx} x2={cx} y1={cy - r} y2={cy + r} stroke="var(--line-1)" strokeWidth="0.5" />
            {data.map((m, i) => {
                // Equal-area projection: r' = R * sqrt(2) * sin((90-dip)/2) ... simplified
                const dipRad = (m.dip * Math.PI) / 180;
                const dirRad = (m.dip_direction * Math.PI) / 180;
                const rho = r * Math.sin((Math.PI / 2 - dipRad) / 2) * Math.SQRT2;
                const x = cx + rho * Math.sin(dirRad);
                const y = cy - rho * Math.cos(dirRad);
                return <circle key={i} cx={x} cy={y} r={2.2} fill="var(--accent)" opacity={0.85} />;
            })}
            <text x={cx} y={cy - r - 2} fill="var(--fg-3)" fontSize="9" textAnchor="middle" fontFamily="var(--font-mono)">N</text>
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
            <text x={cx} y={cy - r - 2} fill="var(--fg-3)" fontSize="9" textAnchor="middle" fontFamily="var(--font-mono)">N</text>
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

export function DownholeMultiLog({ tracks, depthMax = 600, height = 360, trackWidth = 80 }: {
    tracks?: DownholeTrack[];
    depthMax?: number;
    height?: number;
    trackWidth?: number;
}) {
    const data = tracks ?? [];
    if (data.length === 0) {
        return <div className="text-[11px] font-mono p-4 text-center" style={{ color: 'var(--fg-3)' }}>No log curves loaded.</div>;
    }
    const width = data.length * (trackWidth + 6) + 40;
    return (
        <svg width={width} height={height + 20} viewBox={`0 0 ${width} ${height + 20}`}>
            {/* Depth axis */}
            <text x={4} y={12} fill="var(--fg-3)" fontSize="9" fontFamily="var(--font-mono)">DEPTH (m)</text>
            {[0, 0.25, 0.5, 0.75, 1].map((p) => (
                <g key={p}>
                    <line x1={36} y1={p * height + 16} x2={width} y2={p * height + 16} stroke="var(--line-1)" strokeDasharray="2 2" strokeWidth="0.4" />
                    <text x={4} y={p * height + 20} fill="var(--fg-3)" fontSize="9" fontFamily="var(--font-mono)">{Math.round(p * depthMax)}</text>
                </g>
            ))}
            {data.map((t, i) => {
                const x0 = 40 + i * (trackWidth + 6);
                const range = t.max - t.min || 1;
                const d = t.points.map((p, j) => {
                    const x = x0 + ((p.value - t.min) / range) * trackWidth;
                    const y = 16 + (p.depth / depthMax) * height;
                    return `${j === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
                }).join(' ');
                return (
                    <g key={i}>
                        <rect x={x0} y={16} width={trackWidth} height={height} fill="var(--bg-2)" stroke="var(--line-1)" strokeWidth="0.5" />
                        <text x={x0 + trackWidth / 2} y={12} fill="var(--fg-2)" fontSize="9" textAnchor="middle" fontFamily="var(--font-mono)">{t.label}</text>
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
    color: string;
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
    depthMax,
    height = 520,
    width = 220,
}: {
    intervals: LithologyInterval[];
    holeId: string | null;
    depthMax: number;
    height?: number;
    width?: number;
}) {
    if (!intervals.length) {
        return (
            <div className="text-[11px] font-mono p-4 text-center" style={{ color: 'var(--fg-3)', background: 'var(--bg-1)', border: '1px solid var(--line-1)', borderRadius: 6 }}>
                No derived lithology for this hole.
            </div>
        );
    }
    const padT = 32;
    const padB = 14;
    const usableH = height - padT - padB;
    // Fit the depth axis to this hole's actual data + a small buffer,
    // rounded up to a friendly tick. Without this a 130 m hole on a
    // 300 m global axis leaves half the column visually empty.
    const dataMax = intervals[intervals.length - 1].to;
    const denom = roundUpDepth(dataMax);
    const oreCount = intervals.filter((i) => i.code.endsWith('-ORE')).length;

    // Layout: left depth axis (40px) + band column (rest)
    const axisW = 40;
    const bandX = axisW + 4;
    const bandW = width - bandX - 6;

    // Grid lines every 25m up to denom, capped at 12 lines to avoid clutter.
    const gridStepM = denom > 600 ? 100 : denom > 300 ? 50 : 25;
    const gridLines: number[] = [];
    for (let d = gridStepM; d < denom; d += gridStepM) {
        gridLines.push(d);
    }

    // Build a unique legend of lithology codes seen in this hole.
    const legendCodes = Array.from(new Set(intervals.map((i) => i.code)));
    const legendColors = new Map<string, string>();
    intervals.forEach((iv) => {
        if (!legendColors.has(iv.code)) legendColors.set(iv.code, iv.color);
    });

    return (
        <div style={{ background: 'var(--bg-1)', border: '1px solid var(--line-1)', borderRadius: 6, padding: 10, width: width + 20 }}>
            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                Lithology · derived
            </div>
            <div className="text-[10px] font-mono" style={{ color: 'var(--fg-2)' }}>
                {holeId ?? '—'} · {intervals.length} bands · {oreCount} U-host
            </div>
            {/* Legend — moved from below the SVG so the colour key is right
                under the header, before the user's eye reaches the bars. */}
            <div className="mt-1.5 mb-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] font-mono" style={{ color: 'var(--fg-2)' }}>
                {legendCodes.map((code) => {
                    const short = LITHO_SHORT[code] ?? code.replace('DERIVED-', '');
                    const isOre = code.endsWith('-ORE');
                    return (
                        <span key={code} className="flex items-center gap-1.5">
                            <span style={{ display: 'inline-block', width: 10, height: 10, background: legendColors.get(code), border: '1px solid rgba(0,0,0,0.25)' }} />
                            <span style={{ color: isOre ? '#8fe28b' : 'var(--fg-2)', fontWeight: isOre ? 600 : 400 }}>{short}</span>
                        </span>
                    );
                })}
            </div>
            <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{ display: 'block' }}>
                {/* Header */}
                <text x={axisW / 2} y={padT - 8} textAnchor="middle" fontSize="9" fill="var(--fg-3)" fontFamily="var(--font-mono)">
                    DEPTH (m)
                </text>
                <text x={bandX + bandW / 2} y={padT - 8} textAnchor="middle" fontSize="9" fill="var(--fg-3)" fontFamily="var(--font-mono)">
                    LITHOLOGY
                </text>

                {/* Depth grid lines across the band area */}
                {gridLines.map((d) => {
                    const y = padT + (d / denom) * usableH;
                    return (
                        <g key={`grid-${d}`}>
                            <line x1={axisW - 2} y1={y} x2={width} y2={y} stroke="var(--line-1)" strokeWidth="0.5" strokeDasharray="2 3" opacity="0.5" />
                            <text x={axisW - 4} y={y + 3} textAnchor="end" fontSize="9" fill="var(--fg-3)" fontFamily="var(--font-mono)">{d}</text>
                        </g>
                    );
                })}

                {/* Lithology bands */}
                {intervals.map((iv, i) => {
                    const y1 = padT + (iv.from / denom) * usableH;
                    const y2 = padT + (iv.to / denom) * usableH;
                    const h = Math.max(0.5, y2 - y1);
                    const isOre = iv.code.endsWith('-ORE');
                    const short = LITHO_SHORT[iv.code] ?? iv.code.replace('DERIVED-', '');
                    return (
                        <g key={i}>
                            <rect
                                x={bandX}
                                y={y1}
                                width={bandW}
                                height={h}
                                fill={iv.color}
                                stroke={isOre ? '#fff' : 'rgba(0,0,0,0.20)'}
                                strokeWidth={isOre ? '0.8' : '0.4'}
                            />
                            {h >= 12 && (
                                <text
                                    x={bandX + 6}
                                    y={y1 + h / 2 + 3}
                                    fontSize="9"
                                    fill={isOre ? '#1a1a1a' : '#1a1a1a'}
                                    fontFamily="var(--font-mono)"
                                    fontWeight={isOre ? '700' : '500'}
                                >
                                    {short}
                                </text>
                            )}
                            {h >= 14 && (
                                <text
                                    x={bandX + bandW - 6}
                                    y={y1 + h / 2 + 3}
                                    textAnchor="end"
                                    fontSize="8"
                                    fill="rgba(0,0,0,0.65)"
                                    fontFamily="var(--font-mono)"
                                >
                                    {iv.from.toFixed(0)}–{iv.to.toFixed(0)}
                                </text>
                            )}
                        </g>
                    );
                })}
            </svg>
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
        return <div className="text-[11px] font-mono p-4 text-center" style={{ color: 'var(--fg-3)' }}>No stratigraphic units loaded.</div>;
    }
    const padT = 20;
    const padB = 12;
    const usableH = height - padT - padB;
    // Equal-thickness slots; could weight by age-span later.
    const slotH = usableH / units.length;

    return (
        <div style={{ background: 'var(--bg-1)', border: '1px solid var(--line-1)', borderRadius: 6, padding: 12 }}>
            {eyebrow && (
                <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>{eyebrow}</div>
            )}
            <div className="text-xs font-medium mb-2" style={{ color: 'var(--fg-0)' }}>{title}</div>
            <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{ display: 'block' }}>
                <g style={{ fontFamily: 'var(--font-mono)', fontSize: 9 }}>
                    <text x={42} y={padT - 6} textAnchor="middle" fill="var(--fg-3)">AGE</text>
                    <text x={width / 2 + 20} y={padT - 6} textAnchor="middle" fill="var(--fg-3)">UNIT</text>
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
                            <text x={45} y={y + slotH / 2 + 3} textAnchor="middle" fontSize="9.5" fill="oklch(0.18 0.04 50)" fontFamily="var(--font-mono)" fontWeight="500">
                                {u.age}
                            </text>
                            <text x={92} y={y + 14} fontSize="11" fill="var(--fg-0)" fontWeight={u.is_host ? '600' : '500'}>
                                {u.unit_name}
                            </text>
                            {u.age_period && (
                                <text x={92} y={y + 26} fontSize="9" fill="var(--fg-3)" fontFamily="var(--font-mono)">
                                    {u.age_period}{u.lithology ? ` · ${u.lithology}` : ''}
                                </text>
                            )}
                            {(u.notes ?? []).slice(0, 2).map((n, j) => (
                                <text key={j} x={92} y={y + 40 + j * 12} fontSize="9.5" fill="var(--fg-2)">· {n}</text>
                            ))}
                            {u.is_host && (
                                <>
                                    <circle cx={width - 16} cy={y + slotH / 2} r="5" fill="oklch(0.82 0.18 145)" stroke="var(--bg-0)" strokeWidth="1.2" />
                                    <text x={width - 16} y={y + slotH / 2 + 16} textAnchor="middle" fontSize="8.5" fontFamily="var(--font-mono)" fill="oklch(0.82 0.18 145)">U HOST</text>
                                </>
                            )}
                            {i < units.length - 1 && (
                                <line x1={8} y1={y + slotH} x2={width - 8} y2={y + slotH} stroke="var(--line-2)" strokeWidth="0.6" />
                            )}
                        </g>
                    );
                })}
            </svg>
        </div>
    );
}
