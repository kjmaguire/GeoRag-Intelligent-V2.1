import { useMemo } from 'react';

/**
 * Radius of the primitive circle in the units the stored `stereonet_x/y` are
 * in. promote_silver_to_gold normalises the equal-area projection so the
 * primitive is at 1 (the pole of a vertical plane has |(x, y)| = 1). The circle
 * used to be drawn at sqrt(2), so every pole sat inside the net by a factor of
 * 1.41 and the pole of a vertical structure did not reach the rim (GIS audit
 * 2026-10).
 */
export const PRIMITIVE_RADIUS = 1;

export interface StereonetRow {
    structure_type: string;
    /** East, in primitive radii (|(x, y)| <= 1). Null when the measurement has no orientation. */
    stereonet_x: number | null;
    /** North, in primitive radii. Null when the measurement has no orientation. */
    stereonet_y: number | null;
}

/**
 * Lower-hemisphere equal-area net of the poles stored on
 * `gold.structure_measurements_visual`. North is up; a row with no x/y (no
 * orientation recorded) is not drawn - never placed at the centre, which is
 * where the pole of a horizontal bed belongs.
 */
export default function DrillholeStereonet({ points }: { points: StereonetRow[] }) {
    const valid = useMemo(() => points.filter((p) => p.stereonet_x != null && p.stereonet_y != null), [points]);
    return (
        <div className="flex justify-center">
            <svg viewBox="-1.15 -1.15 2.3 2.3" className="w-64 h-64" data-testid="drillhole-stereonet">
                <circle
                    data-testid="stereonet-primitive"
                    cx={0}
                    cy={0}
                    r={PRIMITIVE_RADIUS}
                    fill="none"
                    stroke="var(--line-2)"
                    strokeWidth={0.02}
                />
                <line
                    x1={0}
                    y1={-PRIMITIVE_RADIUS}
                    x2={0}
                    y2={PRIMITIVE_RADIUS}
                    stroke="var(--line-1)"
                    strokeWidth={0.01}
                />
                <line
                    x1={-PRIMITIVE_RADIUS}
                    y1={0}
                    x2={PRIMITIVE_RADIUS}
                    y2={0}
                    stroke="var(--line-1)"
                    strokeWidth={0.01}
                />
                {valid.map((p, i) => (
                    <circle
                        key={i}
                        data-testid="stereonet-pole"
                        cx={p.stereonet_x as number}
                        cy={-(p.stereonet_y as number)}
                        r={0.025}
                        fill={
                            p.structure_type === 'fault'
                                ? '#dc2626'
                                : p.structure_type === 'bedding'
                                  ? '#2563eb'
                                  : 'var(--fg-1)'
                        }
                    />
                ))}
            </svg>
        </div>
    );
}
