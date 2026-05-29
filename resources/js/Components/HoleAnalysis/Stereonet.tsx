import { useMemo, useState } from 'react';

interface Structure {
    depth: number;
    structure_type: string;
    true_dip: number | null;
    dip_direction: number | null;
    description?: string | null;
}

interface StereonetProps {
    structures: Structure[];
    holeId: string;
    /** Optional map of type → visible flag from the parent's checkbox list. */
    visibleTypes?: Record<string, boolean>;
}

/**
 * Lower-hemisphere equal-area (Schmidt) stereonet, rendered as SVG.
 *
 * Plots:
 *   - Great circles for planar features (bedding, foliation, fault,
 *     shear, vein, joint, fracture) using true_dip + dip_direction.
 *   - Poles (dots) at the pole-to-plane position for each feature.
 *
 * Convention:
 *   - North at the top, East on the right.
 *   - A horizontal plane plots as the primitive circle (full net).
 *   - A vertical plane plots as a straight line through the centre.
 *   - The pole to a plane is 90° from the plane itself, plotted as a
 *     point at the projected position of the plane's normal.
 *
 * Equal-area projection formula (Schmidt net):
 *     r / R = √2 · sin((90° - plunge) / 2)
 * where `plunge` is the angle down from horizontal of the line being
 * projected and r is the distance from the centre of the net.
 *
 * No external math library is used — everything is vanilla SVG so the
 * component is ~6 KB gzipped.
 */

const TYPE_COLORS: Record<string, string> = {
    bedding:   '#3b82f6',  // blue
    foliation: '#a855f7',  // purple
    fault:     '#ef4444',  // red
    shear:     '#f97316',  // orange
    joint:     '#14b8a6',  // teal
    fracture:  '#eab308',  // yellow
    vein:      '#22c55e',  // green
    lineation: '#ec4899',  // pink
};

const R = 140;          // primitive radius, px
const PAD = 24;         // padding around the net
const SIZE = 2 * (R + PAD);
const CX = R + PAD;
const CY = R + PAD;

/**
 * Project a trend/plunge line onto the equal-area lower hemisphere.
 * `trendDeg` is 0° = north, measured clockwise.
 * `plungeDeg` is 0° = horizontal, 90° = vertical down.
 * Returns SVG-space (x, y) with north-up.
 */
function projectLine(trendDeg: number, plungeDeg: number): { x: number; y: number } {
    // Equal-area radius fraction.
    const halfCoplunge = (90 - plungeDeg) / 2;
    const rFrac = Math.SQRT2 * Math.sin((halfCoplunge * Math.PI) / 180);
    const r = rFrac * R;

    // Trend: 0° = +y (north), 90° = +x (east). SVG y-axis points down so
    // north-up means we negate y.
    const theta = (trendDeg * Math.PI) / 180;
    const x = CX + r * Math.sin(theta);
    const y = CY - r * Math.cos(theta);
    return { x, y };
}

/**
 * Return the dip direction and dip of the pole to a plane. The pole is
 * the line perpendicular to the plane: its trend is opposite the dip
 * direction, and its plunge is 90° minus the dip.
 */
function poleOfPlane(dipDirection: number, dip: number): { trend: number; plunge: number } {
    const poleTrend = (dipDirection + 180) % 360;
    const polePlunge = 90 - dip;
    return { trend: poleTrend, plunge: polePlunge };
}

/**
 * Sample a great circle (plane) and return a list of projected points
 * describing an SVG polyline. The plane is specified by its dip +
 * dip_direction. The circle is the intersection of the plane with the
 * lower hemisphere.
 */
function planeGreatCircle(dipDirection: number, dip: number, nSamples = 80): { x: number; y: number }[] {
    // Parametrise by the angle along the plane's strike line.
    // A plane with dip δ and dip direction α can be thought of as a
    // rotated primitive circle. For each t in [0, π], compute the
    // trend/plunge of the point on the great circle at rake t from the
    // strike line.
    const strike = (dipDirection - 90 + 360) % 360;  // strike is 90° CCW from dip dir
    const dipRad = (dip * Math.PI) / 180;
    const points: { x: number; y: number }[] = [];

    for (let i = 0; i <= nSamples; i++) {
        const t = (i / nSamples) * Math.PI;  // 0 → π, walking along the plane
        // Direction cosines of the point on the plane in a local frame
        // where x = strike, y = horizontal-perp-to-strike (pointing dip
        // direction), z = up.
        const lx = Math.cos(t);              // along strike
        const ly = Math.sin(t) * Math.cos(dipRad);  // toward dip dir, horiz component
        const lz = -Math.sin(t) * Math.sin(dipRad); // down

        // Rotate from local frame to geographic (N, E, down) frame.
        // Local +x = strike direction; +y = dip-direction horizontal; +z = up.
        // In geographic: strike is (-sin(strike+90°), cos(strike+90°)) =
        // (−sin α, cos α) since strike = α − 90.
        const strikeRad = (strike * Math.PI) / 180;
        const dirRad = (dipDirection * Math.PI) / 180;

        // Unit vector for local +x in geographic (N, E) plane:
        const nStrike = Math.cos(strikeRad);
        const eStrike = Math.sin(strikeRad);
        // Local +y (horizontal in dip direction):
        const nDip = Math.cos(dirRad);
        const eDip = Math.sin(dirRad);

        const nGeo = lx * nStrike + ly * nDip;
        const eGeo = lx * eStrike + ly * eDip;
        const dGeo = -lz;  // down component (positive below horizon)

        // If the point is on the upper hemisphere (dGeo < 0), skip —
        // lower-hemisphere nets ignore upper-hemi sample points.
        if (dGeo < 0) continue;

        // Convert direction cosines back to trend + plunge.
        const horizLen = Math.hypot(nGeo, eGeo);
        const plungeRad = Math.atan2(dGeo, horizLen);
        const plungeDeg = (plungeRad * 180) / Math.PI;
        let trendRad = Math.atan2(eGeo, nGeo);
        let trendDeg = (trendRad * 180) / Math.PI;
        if (trendDeg < 0) trendDeg += 360;

        points.push(projectLine(trendDeg, plungeDeg));
    }
    return points;
}

export default function Stereonet({ structures, holeId, visibleTypes }: StereonetProps) {
    const [hovered, setHovered] = useState<number | null>(null);

    const { elements, counts, planarCount, linearCount } = useMemo(() => {
        const els: React.ReactElement[] = [];
        const cnt: Record<string, number> = {};

        let planarN = 0;
        let linearN = 0;

        // Primitive circle + compass ticks (drawn by caller via static JSX).
        // Note: we draw the grid separately so hover state doesn't re-render it.

        for (let i = 0; i < structures.length; i++) {
            const s = structures[i];
            const type = s.structure_type;
            cnt[type] = (cnt[type] || 0) + 1;

            if (visibleTypes && visibleTypes[type] === false) continue;

            const color = TYPE_COLORS[type] ?? '#94a3b8';

            if (s.true_dip == null || s.dip_direction == null) continue;

            if (type === 'lineation') {
                // Lineation: a line, plotted as a single projected point
                // where the trend = dip_direction and the plunge =
                // true_dip (field geologists record those fields exactly
                // this way for lineations).
                const { x, y } = projectLine(s.dip_direction, s.true_dip);
                els.push(
                    <circle
                        key={`pt-${i}`}
                        cx={x}
                        cy={y}
                        r={hovered === i ? 5 : 3.2}
                        fill={color}
                        stroke="rgba(0,0,0,0.4)"
                        strokeWidth={0.5}
                        className="cursor-pointer transition-all"
                        onMouseEnter={() => setHovered(i)}
                        onMouseLeave={() => setHovered(null)}
                    >
                        <title>{s.description ?? `${type} ${s.true_dip.toFixed(0)}°/${s.dip_direction.toFixed(0)}° @ ${s.depth.toFixed(1)}m`}</title>
                    </circle>,
                );
                linearN++;
            } else {
                // Planar structure: draw the great circle + the pole.
                planarN++;
                const points = planeGreatCircle(s.dip_direction, s.true_dip);
                if (points.length > 1) {
                    const d = points.map((p, idx) => `${idx === 0 ? 'M' : 'L'} ${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' ');
                    els.push(
                        <path
                            key={`gc-${i}`}
                            d={d}
                            fill="none"
                            stroke={color}
                            strokeWidth={hovered === i ? 2 : 1}
                            strokeOpacity={hovered === i ? 1 : 0.45}
                            className="cursor-pointer transition-all"
                            onMouseEnter={() => setHovered(i)}
                            onMouseLeave={() => setHovered(null)}
                        >
                            <title>{s.description ?? `${type} ${s.true_dip.toFixed(0)}°/${s.dip_direction.toFixed(0)}° @ ${s.depth.toFixed(1)}m`}</title>
                        </path>,
                    );
                }

                const pole = poleOfPlane(s.dip_direction, s.true_dip);
                const { x, y } = projectLine(pole.trend, pole.plunge);
                els.push(
                    <circle
                        key={`pole-${i}`}
                        cx={x}
                        cy={y}
                        r={hovered === i ? 4 : 2.4}
                        fill={color}
                        fillOpacity={0.9}
                        className="pointer-events-none transition-all"
                    />,
                );
            }
        }

        return { elements: els, counts: cnt, planarCount: planarN, linearCount: linearN };
    }, [structures, hovered, visibleTypes]);

    // Compass ticks every 30°.
    const ticks: React.ReactElement[] = [];
    for (let deg = 0; deg < 360; deg += 30) {
        const { x: x1, y: y1 } = projectLine(deg, 0);  // on primitive
        const rad = (deg * Math.PI) / 180;
        const x2 = CX + (R + 8) * Math.sin(rad);
        const y2 = CY - (R + 8) * Math.cos(rad);
        ticks.push(
            <line
                key={`tick-${deg}`}
                x1={x1}
                y1={y1}
                x2={x2}
                y2={y2}
                stroke="rgba(148,163,184,0.6)"
                strokeWidth={1}
            />
        );
    }

    // Inner grid: small circles at plunges 30, 60 (equal-area radii)
    const innerCircles: number[] = [30, 60].map((p) => Math.SQRT2 * Math.sin(((90 - p) / 2) * Math.PI / 180) * R);

    return (
        <div className="w-full h-full flex flex-col items-center gap-2">
            <svg
                viewBox={`0 0 ${SIZE} ${SIZE}`}
                className="w-full max-w-[360px]"
                role="img"
                aria-label={`Stereonet for drill hole ${holeId}`}
            >
                {/* Background fill */}
                <circle cx={CX} cy={CY} r={R} fill="rgba(15,23,42,0.6)" stroke="rgba(148,163,184,0.8)" strokeWidth={1.4} />
                {/* Inner grid circles */}
                {innerCircles.map((rInner, idx) => (
                    <circle key={`inner-${idx}`} cx={CX} cy={CY} r={rInner} fill="none" stroke="rgba(148,163,184,0.2)" strokeWidth={0.8} />
                ))}
                {/* Cross-hair */}
                <line x1={CX - R} y1={CY} x2={CX + R} y2={CY} stroke="rgba(148,163,184,0.18)" strokeWidth={0.8} />
                <line x1={CX} y1={CY - R} x2={CX} y2={CY + R} stroke="rgba(148,163,184,0.18)" strokeWidth={0.8} />
                {/* Compass ticks */}
                {ticks}
                {/* Cardinal labels */}
                <text x={CX} y={CY - R - 12} textAnchor="middle" fontSize={12} fill="#e2e8f0" fontWeight={600}>N</text>
                <text x={CX + R + 14} y={CY + 4} textAnchor="middle" fontSize={12} fill="#e2e8f0" fontWeight={600}>E</text>
                <text x={CX} y={CY + R + 20} textAnchor="middle" fontSize={12} fill="#e2e8f0" fontWeight={600}>S</text>
                <text x={CX - R - 14} y={CY + 4} textAnchor="middle" fontSize={12} fill="#e2e8f0" fontWeight={600}>W</text>
                {/* Plotted structures */}
                {elements}
            </svg>
            <div className="text-xs text-gray-400 text-center">
                Lower-hemisphere equal-area · {planarCount} planar + {linearCount} linear measurements
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-1 justify-center text-[11px]">
                {Object.entries(counts).map(([type, n]) => (
                    <span key={type} className="flex items-center gap-1.5">
                        <span
                            className="w-2.5 h-2.5 rounded-full"
                            style={{ backgroundColor: TYPE_COLORS[type] ?? '#94a3b8' }}
                        />
                        <span className="text-gray-300">{type}</span>
                        <span className="text-gray-500">({n})</span>
                    </span>
                ))}
            </div>
        </div>
    );
}
