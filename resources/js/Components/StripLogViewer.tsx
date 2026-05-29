import * as React from 'react';
import { useState, useEffect, useRef, useCallback } from 'react';
import { cn } from '../lib/utils';

/**
 * StripLogViewer
 *
 * Renders a vertical SVG strip log for a single drill hole, per Section 04g
 * of the GeoRAG architecture specification.
 *
 * Props:
 *   holeId    {string} - The hole_id string (e.g. "DH-001")
 *   projectId {string} - UUID of the project
 *   onQueryHole {function} - optional callback(queryText) to send a chat query
 */

// V1.5-10 — Local types for the API response shape. Mirror the canonical
// silver.collars + silver.lithology_logs + silver.well_log_curves columns
// without dragging the full @/types tree into this component file. Use
// loose `Record<string, unknown>` for nested rows we don't directly index.
interface LithologyLog {
    log_id?: string;
    // V1.5-10 — actual API uses `from_depth` / `to_depth`; the silver-table
    // names `depth_from_m` / `depth_to_m` are an unreached future shape.
    // Accept both so refactor is non-breaking.
    from_depth?: number;
    to_depth?: number;
    depth_from_m?: number | null;
    depth_to_m?: number | null;
    lithology_code?: string | null;
    description?: string | null;
    [k: string]: unknown;
}
interface WellLogCurve {
    curve_name?: string | null;
    depth_m?: number | null;
    value?: number | null;
    [k: string]: unknown;
}
interface CollarRecord {
    collar_id?: string;
    hole_id?: string;
    project_id?: string;
    hole_type?: string | null;
    drill_date?: string | null;
    azimuth?: number | null;
    dip?: number | null;
    elevation?: number | null;
    total_depth?: number | null;
    lithology_logs?: LithologyLog[];
    well_log_curves?: WellLogCurve[];
    [k: string]: unknown;
}
interface StripLogViewerProps {
    holeId: string | null | undefined;
    projectId: string | null | undefined;
    onQueryHole?: (queryText: string) => void;
}
interface TooltipPos {
    x: number;
    y: number;
}

// ── Lithology colour palette ──────────────────────────────────────────────────
// Colours are derived from standard geological conventions.
// SST  Sandstone      — warm yellow
// CGL  Conglomerate   — tan/brown
// PGN  Pelitic Gneiss — dark gray (medium metamorphic)
// GPT  Graphitic Pelite — near-black (carbonaceous)
// Default                 light gray (unknown / not logged)
const LITHO_COLORS = {
    SST:     { fill: '#d4a843', stroke: '#b8892a', label: 'Sandstone' },
    CGL:     { fill: '#a07850', stroke: '#856038', label: 'Conglomerate' },
    PGN:     { fill: '#5a5a6e', stroke: '#44445a', label: 'Pelitic Gneiss' },
    GPT:     { fill: '#2a2a30', stroke: '#1a1a20', label: 'Graphitic Pelite' },
    DEFAULT: { fill: '#6b7280', stroke: '#4b5563', label: 'Unknown' },
};

function getLithoColor(code) {
    return LITHO_COLORS[code?.toUpperCase()] ?? LITHO_COLORS.DEFAULT;
}

// ── Layout constants ──────────────────────────────────────────────────────────
const DEPTH_AXIS_WIDTH  = 70;   // px — left depth scale column
const LITHO_COL_WIDTH   = 160;  // px — lithology rectangles
const CURVE_COL_WIDTH   = 180;  // px — LAS continuous curves column (GR, RHOB)
const DETAIL_COL_WIDTH  = 180;  // px — RQD/recovery bars column
const STRIP_HEIGHT      = 600;  // px — default SVG height (fills container via viewBox)
const HEADER_HEIGHT     = 0;    // handled in HTML, not SVG
const TICK_INTERVAL     = 10;   // m  — depth tick every N metres
const FONT_FAMILY       = 'ui-monospace, SFMono-Regular, Menlo, monospace';
const FONT_SIZE_TICK    = 10;   // depth tick labels
const FONT_SIZE_TICK_MAJOR = 12; // major tick labels (every 50m)
const FONT_SIZE_CODE    = 11;   // lithology code inside bars
const FONT_SIZE_AXIS    = 10;   // "DEPTH (m)" axis label

// ── Utility ───────────────────────────────────────────────────────────────────
function depthToY(depth, totalDepth, svgHeight) {
    return (depth / totalDepth) * svgHeight;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function LoadingState() {
    return (
        <div className="flex-1 flex items-center justify-center py-20">
            <div className="flex flex-col items-center gap-3">
                <div className="w-6 h-6 rounded-full border-2 border-gray-600 border-t-amber-400 animate-spin" />
                <span className="text-xs text-gray-500">Loading lithology…</span>
            </div>
        </div>
    );
}

function EmptyState({ holeId }) {
    return (
        <div className="flex-1 flex items-center justify-center py-20 text-center px-6">
            <div className="space-y-2">
                <p className="text-sm text-gray-400">
                    {holeId ? `No lithology data for ${holeId}` : 'Select a drill hole to view the strip log.'}
                </p>
                {holeId && (
                    <p className="text-xs text-gray-600">
                        Lithology logs may not yet be imported for this hole.
                    </p>
                )}
            </div>
        </div>
    );
}

function ErrorState({ message }) {
    return (
        <div className="mx-4 mt-4 text-xs text-red-400 bg-red-950/40 border border-red-800/40 rounded px-3 py-2" role="alert">
            Error loading strip log: {message}
        </div>
    );
}

/**
 * Depth tick marks on the left axis.
 */
function DepthAxis({ totalDepth, svgHeight, horizontal = false }: { totalDepth: number; svgHeight: number; horizontal?: boolean }) {
    const ticks: React.ReactElement[] = [];
    for (let d = 0; d <= totalDepth; d += TICK_INTERVAL) {
        const y = depthToY(d, totalDepth, svgHeight);
        const isMajor = d % 50 === 0;
        const tx = DEPTH_AXIS_WIDTH - 9;
        const ty = y + 4;

        ticks.push(
            <g key={d}>
                <line
                    x1={DEPTH_AXIS_WIDTH - 6}
                    y1={y}
                    x2={DEPTH_AXIS_WIDTH}
                    y2={y}
                    stroke="#6b7280"
                    strokeWidth={isMajor ? 1.5 : 1}
                />
                <text
                    x={tx}
                    y={ty}
                    textAnchor="end"
                    fontSize={isMajor ? FONT_SIZE_TICK_MAJOR + 1 : FONT_SIZE_TICK}
                    fill={isMajor ? '#d1d5db' : '#6b7280'}
                    fontFamily={FONT_FAMILY}
                    fontWeight={isMajor ? '600' : '400'}
                    transform={horizontal ? `rotate(-90, ${tx}, ${ty})` : undefined}
                >
                    {d}
                </text>
            </g>,
        );
    }

    return (
        <g aria-label="Depth axis">
            {/* Axis label */}
            <text
                x={12}
                y={svgHeight / 2}
                textAnchor="middle"
                fontSize={FONT_SIZE_AXIS + 1}
                fill="#9ca3af"
                fontFamily={FONT_FAMILY}
                transform={horizontal
                    ? `rotate(-90, 12, ${svgHeight / 2})`
                    : `rotate(-90, 12, ${svgHeight / 2})`
                }
                letterSpacing="1"
            >
                DEPTH (m)
            </text>
            {/* Vertical rule */}
            <line
                x1={DEPTH_AXIS_WIDTH}
                y1={0}
                x2={DEPTH_AXIS_WIDTH}
                y2={svgHeight}
                stroke="#374151"
                strokeWidth={1}
            />
            {ticks}
        </g>
    );
}

/**
 * Horizontal grid line at every major tick (50 m).
 */
function GridLines({ totalDepth, svgHeight, svgWidth }: { totalDepth: number; svgHeight: number; svgWidth: number }) {
    const lines: React.ReactElement[] = [];
    for (let d = 0; d <= totalDepth; d += 50) {
        const y = depthToY(d, totalDepth, svgHeight);
        lines.push(
            <line
                key={d}
                x1={DEPTH_AXIS_WIDTH}
                y1={y}
                x2={svgWidth}
                y2={y}
                stroke="#1f2937"
                strokeWidth={1}
                strokeDasharray="4 2"
            />,
        );
    }
    return <g aria-hidden="true">{lines}</g>;
}

/**
 * Lithology column — coloured rectangles.
 */
function LithologyColumn({ intervals, totalDepth, svgHeight, onIntervalHover, onIntervalLeave, hoveredLogId }) {
    const x = DEPTH_AXIS_WIDTH;

    return (
        <g aria-label="Lithology column">
            {/* Column background */}
            <rect
                x={x}
                y={0}
                width={LITHO_COL_WIDTH}
                height={svgHeight}
                fill="#111827"
            />

            {intervals.map((interval) => {
                const y1 = depthToY(interval.from_depth, totalDepth, svgHeight);
                const y2 = depthToY(interval.to_depth, totalDepth, svgHeight);
                const h  = Math.max(y2 - y1, 1);
                const { fill, stroke } = getLithoColor(interval.lithology_code);
                const isHovered = hoveredLogId === interval.log_id;

                return (
                    <rect
                        key={interval.log_id}
                        x={x + 2}
                        y={y1}
                        width={LITHO_COL_WIDTH - 4}
                        height={h}
                        fill={fill}
                        stroke={isHovered ? '#f59e0b' : stroke}
                        strokeWidth={isHovered ? 1.5 : 0.5}
                        style={{ cursor: 'pointer' }}
                        onMouseEnter={(e) => onIntervalHover(interval, e)}
                        onMouseLeave={onIntervalLeave}
                        role="listitem"
                        aria-label={`${interval.lithology_code} ${interval.from_depth}–${interval.to_depth} m`}
                    />
                );
            })}

            {/* Column header rule */}
            <rect
                x={x}
                y={0}
                width={LITHO_COL_WIDTH}
                height={1}
                fill="#374151"
            />
        </g>
    );
}

/**
 * Lithology code labels inside the column (for tall enough intervals).
 */
function LithologyLabels({ intervals, totalDepth, svgHeight, horizontal = false }) {
    const x = DEPTH_AXIS_WIDTH;
    const MIN_HEIGHT_FOR_LABEL = 12;

    return (
        <g aria-hidden="true">
            {intervals.map((interval) => {
                const y1 = depthToY(interval.from_depth, totalDepth, svgHeight);
                const y2 = depthToY(interval.to_depth, totalDepth, svgHeight);
                const h  = y2 - y1;

                if (h < MIN_HEIGHT_FOR_LABEL) return null;

                const cx = x + LITHO_COL_WIDTH / 2;
                const cy = y1 + h / 2;
                const fs = Math.min(FONT_SIZE_CODE + 2, h * 0.35);

                // In horizontal mode the whole SVG is rotated 90° CW,
                // so we counter-rotate text -90° to keep it upright/readable.
                const transform = horizontal
                    ? `rotate(-90, ${cx}, ${cy})`
                    : undefined;

                return (
                    <text
                        key={interval.log_id}
                        x={cx}
                        y={cy + 4}
                        textAnchor="middle"
                        fontSize={fs}
                        fill="rgba(255,255,255,0.9)"
                        fontFamily={FONT_FAMILY}
                        fontWeight="700"
                        transform={transform}
                        style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                        {interval.lithology_code}
                    </text>
                );
            })}
        </g>
    );
}

// ── Curve colour palette ──────────────────────────────────────────────────────
const CURVE_COLORS = {
    GR:   { stroke: '#22c55e', label: 'Gamma Ray' },     // green
    RHOB: { stroke: '#3b82f6', label: 'Density' },       // blue
    NPHI: { stroke: '#f59e0b', label: 'Neutron Porosity' }, // amber
    SP:   { stroke: '#ef4444', label: 'Spontaneous Potential' }, // red
};

function getCurveColor(name) {
    return CURVE_COLORS[name?.toUpperCase()] ?? { stroke: '#9ca3af', label: name || '?' };
}

/**
 * Continuous LAS curve traces — SVG polylines rendered in a column after lithology.
 */
function CurveTraces({ curves, totalDepth, svgHeight }) {
    if (!curves || curves.length === 0) return null;

    const x = DEPTH_AXIS_WIDTH + LITHO_COL_WIDTH;
    const w = CURVE_COL_WIDTH;

    return (
        <g aria-label="Well log curves">
            {/* Column background */}
            <rect x={x} y={0} width={w} height={svgHeight} fill="#0a0a0f" />
            {/* Column separator */}
            <line x1={x} y1={0} x2={x} y2={svgHeight} stroke="#1f2937" strokeWidth={1} />

            {curves.map((curve) => {
                const { stroke } = getCurveColor(curve.curve_name);
                const depths = curve.depths ?? [];
                const values = curve.values ?? [];
                if (depths.length === 0) return null;

                // Find min/max of values to normalize within the column width.
                const validVals = values.filter((v) => v !== curve.null_value && v != null);
                if (validVals.length === 0) return null;
                const vMin = Math.min(...validVals);
                const vMax = Math.max(...validVals);
                const vRange = vMax - vMin || 1;

                // Downsample to ~500 points to keep SVG performant.
                const step = Math.max(1, Math.floor(depths.length / 500));

                const points: string[] = [];
                for (let i = 0; i < depths.length; i += step) {
                    const val = values[i];
                    if (val === curve.null_value || val == null) continue;
                    const py = depthToY(depths[i], totalDepth, svgHeight);
                    const px = x + 4 + ((val - vMin) / vRange) * (w - 8);
                    points.push(`${px.toFixed(1)},${py.toFixed(1)}`);
                }

                if (points.length < 2) return null;

                return (
                    <polyline
                        key={curve.curve_name}
                        points={points.join(' ')}
                        fill="none"
                        stroke={stroke}
                        strokeWidth={1.2}
                        opacity={0.85}
                        aria-label={`${curve.curve_name} curve`}
                    />
                );
            })}

            {/* Curve legend in top-left */}
            {curves.map((curve, i) => {
                const { stroke, label } = getCurveColor(curve.curve_name);
                return (
                    <g key={curve.curve_name}>
                        <line
                            x1={x + 6} y1={12 + i * 14}
                            x2={x + 20} y2={12 + i * 14}
                            stroke={stroke} strokeWidth={2}
                        />
                        <text
                            x={x + 24} y={12 + i * 14 + 3}
                            fontSize={10} fill="#9ca3af" fontFamily={FONT_FAMILY}
                        >
                            {curve.curve_name} ({curve.curve_unit || '?'})
                        </text>
                    </g>
                );
            })}
        </g>
    );
}

/**
 * RQD / Recovery bar chart in the detail column.
 * Renders thin horizontal bars proportional to the value (0–100).
 */
function RqdBars({ intervals, totalDepth, svgHeight }) {
    const x    = DEPTH_AXIS_WIDTH + LITHO_COL_WIDTH + CURVE_COL_WIDTH + 4;
    const colW = 60;

    return (
        <g aria-label="RQD and recovery">
            {intervals.map((interval) => {
                const y1 = depthToY(interval.from_depth, totalDepth, svgHeight);
                const y2 = depthToY(interval.to_depth, totalDepth, svgHeight);
                const h  = Math.max(y2 - y1 - 2, 1);

                const rqd      = interval.rqd      != null ? Math.min(100, Math.max(0, interval.rqd))      : null;
                const recovery = interval.recovery != null ? Math.min(100, Math.max(0, interval.recovery)) : null;

                const barH = Math.max(Math.min(h / 2, 6), 2);

                return (
                    <g key={interval.log_id}>
                        {/* RQD bar */}
                        {rqd != null && (
                            <>
                                <rect x={x} y={y1 + 1}           width={colW} height={barH} fill="#1f2937" rx={1} />
                                <rect x={x} y={y1 + 1}           width={(rqd / 100) * colW} height={barH}
                                      fill={rqd >= 75 ? '#22c55e' : rqd >= 50 ? '#f59e0b' : '#ef4444'} rx={1} />
                            </>
                        )}
                        {/* Recovery bar */}
                        {recovery != null && (
                            <>
                                <rect x={x} y={y1 + 1 + barH + 1} width={colW} height={barH} fill="#1f2937" rx={1} />
                                <rect x={x} y={y1 + 1 + barH + 1} width={(recovery / 100) * colW} height={barH}
                                      fill="#3b82f6" rx={1} />
                            </>
                        )}
                    </g>
                );
            })}
        </g>
    );
}

/**
 * Tooltip overlay — rendered as HTML positioned over the SVG container.
 */
function IntervalTooltip({ interval, position, totalDepth }) {
    if (!interval || !position) return null;

    const { fill, label } = getLithoColor(interval.lithology_code);
    const thickness = (interval.to_depth - interval.from_depth).toFixed(1);

    return (
        <div
            className="absolute z-50 pointer-events-none"
            style={{
                left:  position.x + 12,
                top:   Math.max(0, position.y - 10),
                maxWidth: '220px',
            }}
            role="tooltip"
        >
            <div className="bg-gray-900 border border-gray-700 rounded-lg shadow-xl px-3 py-2.5 text-xs space-y-1.5">
                {/* Colour swatch + code */}
                <div className="flex items-center gap-2">
                    <span
                        className="w-3 h-3 rounded-sm shrink-0 border border-gray-600"
                        style={{ background: fill }}
                        aria-hidden="true"
                    />
                    <span className="font-mono font-bold text-gray-100">
                        {interval.lithology_code ?? '?'}
                    </span>
                    <span className="text-gray-400">{label}</span>
                </div>

                {/* Depth range */}
                <div className="text-gray-300 font-mono">
                    {interval.from_depth} – {interval.to_depth} m
                    <span className="ml-2 text-gray-500">({thickness} m)</span>
                </div>

                {/* Description */}
                {interval.lithology_description && (
                    <div className="text-gray-400 leading-relaxed border-t border-gray-700/50 pt-1.5">
                        {interval.lithology_description}
                    </div>
                )}

                {/* RQD / Recovery */}
                {(interval.rqd != null || interval.recovery != null) && (
                    <div className="flex gap-3 border-t border-gray-700/50 pt-1.5">
                        {interval.rqd != null && (
                            <span className="text-gray-400">
                                RQD: <span className="text-gray-200 font-mono">{interval.rqd}%</span>
                            </span>
                        )}
                        {interval.recovery != null && (
                            <span className="text-gray-400">
                                Rec: <span className="text-gray-200 font-mono">{interval.recovery}%</span>
                            </span>
                        )}
                    </div>
                )}

                {/* Weathering */}
                {interval.weathering && (
                    <div className="text-gray-500 text-xs">
                        Weathering: {interval.weathering}
                    </div>
                )}
            </div>
        </div>
    );
}

/**
 * Column header labels rendered above the SVG.
 */
function ColumnHeaders({ hasCurves = false }) {
    return (
        <div
            className="flex text-xs text-gray-500 uppercase tracking-wider border-b border-gray-800 bg-gray-900 shrink-0"
            style={{ paddingLeft: DEPTH_AXIS_WIDTH }}
            aria-hidden="true"
        >
            <div style={{ width: LITHO_COL_WIDTH }} className="px-1 py-1.5 text-center">
                Lithology
            </div>
            {hasCurves && (
                <div style={{ width: CURVE_COL_WIDTH }} className="px-1 py-1.5 text-center">
                    Well Logs
                </div>
            )}
            <div style={{ width: DETAIL_COL_WIDTH }} className="px-1 py-1.5">
                RQD / Recovery
            </div>
        </div>
    );
}

/**
 * Colour legend rendered below the strip log.
 */
function Legend({ usedCodes }) {
    if (!usedCodes || usedCodes.length === 0) return null;

    return (
        <div className="px-4 py-3 border-t border-gray-800 bg-gray-900 shrink-0">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-2">Legend</p>
            <div className="flex flex-wrap gap-3">
                {usedCodes.map((code) => {
                    const { fill, label } = getLithoColor(code);
                    return (
                        <div key={code} className="flex items-center gap-1.5">
                            <span
                                className="w-3 h-3 rounded-sm border border-gray-600"
                                style={{ background: fill }}
                                aria-hidden="true"
                            />
                            <span className="text-xs text-gray-400">
                                <span className="font-mono text-gray-300">{code}</span>
                                {' '}
                                {label}
                            </span>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function StripLogViewer({
    holeId,
    projectId,
    onQueryHole,
}: StripLogViewerProps) {
    // V1.5-10 — typed state. Previous untyped useState(null) inferred `never`
    // and broke every collar.lithology_logs / collar.well_log_curves access.
    const [collar, setCollar]         = useState<CollarRecord | null>(null);
    const [loading, setLoading]       = useState<boolean>(false);
    const [error, setError]           = useState<string | null>(null);
    const [horizontal, setHorizontal] = useState<boolean>(false);

    // Tooltip state
    const [hoveredInterval, setHoveredInterval] = useState<LithologyLog | null>(null);
    const [tooltipPos, setTooltipPos]           = useState<TooltipPos | null>(null);
    const [hoveredLogId, setHoveredLogId]       = useState<string | null>(null);

    const containerRef = useRef<HTMLDivElement | null>(null);

    const fetchCollar = useCallback(async () => {
        if (!projectId || !holeId) {
            setCollar(null);
            return;
        }

        setLoading(true);
        setError(null);

        try {
            // Auth via Sanctum session cookie (same-origin). No bearer token from
            // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
            // The show() endpoint returns collar + lithology_logs eager-loaded.
            // We look up by hole_id so we first need the collar_id from the index.
            // Simpler: hit index with a search, then show. Actually the API uses
            // collar_id in the path, so search by hole_id first.
            const authHeaders = {
                Accept: 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
            };
            const indexRes = await fetch(
                `/api/v1/projects/${projectId}/collars?per_page=200`,
                { credentials: 'same-origin', headers: authHeaders },
            );
            if (!indexRes.ok) throw new Error(`HTTP ${indexRes.status}`);
            const indexBody = await indexRes.json();
            const collarList = indexBody.data ?? indexBody;
            const match = collarList.find((c) => c.hole_id === holeId);
            if (!match) throw new Error(`Collar ${holeId} not found in project.`);

            // Now fetch the full record with lithology
            const showRes = await fetch(
                `/api/v1/projects/${projectId}/collars/${match.collar_id}`,
                { credentials: 'same-origin', headers: authHeaders },
            );
            if (!showRes.ok) throw new Error(`HTTP ${showRes.status}`);
            const showBody = await showRes.json();
            setCollar(showBody.data ?? showBody);
        } catch (err) {
            // V1.5-10 — narrow `unknown` so .message is type-safe.
            setError(err instanceof Error ? err.message : String(err));
        } finally {
            setLoading(false);
        }
    }, [projectId, holeId]);

    useEffect(() => {
        fetchCollar();
    }, [fetchCollar]);

    function handleIntervalHover(interval, svgEvent) {
        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) return;
        setHoveredInterval(interval);
        setHoveredLogId(interval.log_id);
        setTooltipPos({
            x: svgEvent.clientX - rect.left,
            y: svgEvent.clientY - rect.top,
        });
    }

    function handleIntervalLeave() {
        setHoveredInterval(null);
        setHoveredLogId(null);
        setTooltipPos(null);
    }

    // ── Render helpers ────────────────────────────────────────────────────────

    const lithologyLogs = collar?.lithology_logs ?? [];
    const wellLogCurves = collar?.well_log_curves ?? [];
    const totalDepth    = collar?.total_depth ?? 0;

    // Sort intervals top-to-bottom
    const sortedLogs = [...lithologyLogs].sort(
        (a, b) => ((a.from_depth ?? 0) as number) - ((b.from_depth ?? 0) as number),
    );

    // Derive the set of unique codes used (for legend)
    const usedCodes = [...new Set(sortedLogs.map((l) => l.lithology_code).filter(Boolean))];

    // SVG dimensions — add curve column width only if curves exist
    const hasCurves = wellLogCurves.length > 0;
    const svgHeight = Math.min(Math.max(totalDepth * 8, 400), 1200);
    const svgWidth  = DEPTH_AXIS_WIDTH + LITHO_COL_WIDTH
                    + (hasCurves ? CURVE_COL_WIDTH : 0)
                    + DETAIL_COL_WIDTH;

    // ── Early return states ───────────────────────────────────────────────────

    if (loading) return <LoadingState />;
    if (error)   return <ErrorState message={error} />;
    if (!holeId || !collar) return <EmptyState holeId={holeId} />;
    if (lithologyLogs.length === 0) return <EmptyState holeId={holeId} />;

    // ── Full render ───────────────────────────────────────────────────────────

    const drillDate = collar.drill_date
        ? new Date(collar.drill_date).toLocaleDateString('en-CA')
        : '—';

    return (
        <div className="flex flex-col h-full bg-gray-950 overflow-hidden">

            {/* ── Header: hole metadata ── */}
            <div className="px-4 py-3 border-b border-gray-800 bg-gray-900 shrink-0">
                <div className="flex items-start justify-between gap-4">
                    <div>
                        <h2 className="text-base font-bold text-gray-100 font-mono">
                            {collar.hole_id}
                        </h2>
                        <p className="text-xs text-gray-500 mt-0.5">
                            {collar.hole_type ?? '—'}
                            {collar.total_depth != null && (
                                <span className="ml-2">
                                    {collar.total_depth.toFixed(1)} m TD
                                </span>
                            )}
                            {collar.drill_date && (
                                <span className="ml-2">{drillDate}</span>
                            )}
                        </p>
                    </div>

                    {/* Orientation toggle + Azimuth / Dip / Elevation */}
                    <div className="flex gap-2 flex-wrap justify-end items-center">
                        <button
                            type="button"
                            onClick={() => setHorizontal(!horizontal)}
                            className={cn(
                                'text-xs px-2 py-1 rounded border transition-colors',
                                horizontal
                                    ? 'text-amber-400 border-amber-700 bg-amber-950/40'
                                    : 'text-gray-400 border-gray-700 hover:text-gray-200 hover:border-gray-600',
                            )}
                            title={horizontal ? 'Switch to vertical' : 'Switch to horizontal'}
                        >
                            {horizontal ? '↔ Horizontal' : '↕ Vertical'}
                        </button>
                        {collar.azimuth != null && (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-800 border border-gray-700 rounded text-xs font-mono text-gray-300">
                                <span className="text-gray-500 text-[10px]">AZ</span>
                                {collar.azimuth.toFixed(1)}°
                            </span>
                        )}
                        {collar.dip != null && (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-800 border border-gray-700 rounded text-xs font-mono text-gray-300">
                                <span className="text-gray-500 text-[10px]">DIP</span>
                                {collar.dip.toFixed(1)}°
                            </span>
                        )}
                        {collar.elevation != null && (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-800 border border-gray-700 rounded text-xs font-mono text-gray-300">
                                <span className="text-gray-500 text-[10px]">ELEV</span>
                                {collar.elevation.toFixed(0)} m
                            </span>
                        )}
                    </div>
                </div>

                {/* Ask about this hole shortcut */}
                {onQueryHole && (
                    <button
                        type="button"
                        onClick={() => onQueryHole(`Summarise the lithology intersections for drill hole ${collar.hole_id}`)}
                        className={cn(
                            'mt-2 text-xs text-amber-400 hover:text-amber-300',
                            'border border-amber-800/50 hover:border-amber-600/50',
                            'bg-amber-950/30 hover:bg-amber-950/50',
                            'rounded px-2 py-1 transition-colors duration-150',
                            'focus:outline-none focus:ring-1 focus:ring-amber-500',
                        )}
                    >
                        Ask GeoRAG about this hole
                    </button>
                )}
            </div>

            {/* ── Column headers ── */}
            <ColumnHeaders hasCurves={hasCurves} />

            {/* ── SVG strip log ── */}
            <div
                ref={containerRef}
                className={cn(
                    'flex-1 relative min-h-0 bg-gray-950',
                    horizontal
                        ? 'overflow-x-auto overflow-y-hidden'
                        : 'overflow-y-auto overflow-x-hidden flex justify-center',
                )}
            >
                <svg
                    width={horizontal ? '100%' : '100%'}
                    height={horizontal ? '100%' : svgHeight}
                    viewBox={horizontal
                        ? `0 0 ${svgHeight} ${svgWidth}`
                        : `0 0 ${svgWidth} ${svgHeight}`
                    }
                    preserveAspectRatio={horizontal ? 'xMinYMin meet' : 'xMidYMin meet'}
                    xmlns="http://www.w3.org/2000/svg"
                    aria-label={`Strip log for drill hole ${holeId}`}
                    role="img"
                    style={{ display: 'block' }}
                >
                    {/* When horizontal, rotate all geometry 90° CW so depth runs L→R,
                        then the viewBox swap (svgHeight × svgWidth) maps it to landscape. */}
                    <g transform={horizontal
                        ? `rotate(90, 0, 0) translate(0, -${svgHeight})`
                        : undefined
                    }>
                    {/* Background */}
                    <rect width={svgWidth} height={svgHeight} fill="#030712" />

                    {/* Grid lines */}
                    <GridLines totalDepth={totalDepth} svgHeight={svgHeight} svgWidth={svgWidth} />

                    {/* Depth axis */}
                    <DepthAxis totalDepth={totalDepth} svgHeight={svgHeight} horizontal={horizontal} />

                    {/* Lithology rectangles */}
                    <LithologyColumn
                        intervals={sortedLogs}
                        totalDepth={totalDepth}
                        svgHeight={svgHeight}
                        onIntervalHover={handleIntervalHover}
                        onIntervalLeave={handleIntervalLeave}
                        hoveredLogId={hoveredLogId}
                    />

                    {/* Lithology code text labels */}
                    <LithologyLabels
                        intervals={sortedLogs}
                        totalDepth={totalDepth}
                        svgHeight={svgHeight}
                        horizontal={horizontal}
                    />

                    {/* LAS continuous curves (GR, RHOB, etc.) */}
                    {hasCurves && (
                        <CurveTraces
                            curves={wellLogCurves}
                            totalDepth={totalDepth}
                            svgHeight={svgHeight}
                        />
                    )}

                    {/* RQD / Recovery bars */}
                    <RqdBars
                        intervals={sortedLogs}
                        totalDepth={totalDepth}
                        svgHeight={svgHeight}
                    />

                    {/* RQD column header rule */}
                    <line
                        x1={DEPTH_AXIS_WIDTH + LITHO_COL_WIDTH + (hasCurves ? CURVE_COL_WIDTH : 0)}
                        y1={0}
                        x2={DEPTH_AXIS_WIDTH + LITHO_COL_WIDTH + (hasCurves ? CURVE_COL_WIDTH : 0)}
                        y2={svgHeight}
                        stroke="#1f2937"
                        strokeWidth={1}
                    />
                    </g>{/* close horizontal rotation group */}
                </svg>

                {/* Tooltip overlay (HTML, positioned over SVG) */}
                <IntervalTooltip
                    interval={hoveredInterval}
                    position={tooltipPos}
                    totalDepth={totalDepth}
                />
            </div>

            {/* ── Legend ── */}
            <Legend usedCodes={usedCodes} />
        </div>
    );
}
