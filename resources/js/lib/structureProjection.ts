/**
 * Turning stored structural measurements into things a stereonet can plot.
 *
 * Two tables hold structure, in two different conventions:
 *
 *   silver.structure                     plane attitude — true_dip +
 *                                        dip_direction, as a geologist logs it
 *   gold.structure_measurements_visual   the derived pole — pole_trend_deg +
 *                                        pole_plunge_deg, plus strike_deg
 *
 * A stereonet plots LINES. A plane has to be converted to its pole first,
 * and getting that conversion wrong is not a visible failure: the points
 * still land inside the primitive circle, in a pattern that looks like
 * structural data and describes the wrong rock. Ninety degrees out is the
 * difference between a shallow-dipping bedding set and a steep joint set.
 *
 * So the conversion lives here, in one place, with the tests next to it —
 * rather than inline in a 1,300-line page component where it cannot be
 * checked.
 *
 * Conventions throughout, matching Components/HoleAnalysis/Stereonet:
 *   trend    degrees clockwise from north, 0–360
 *   plunge   degrees down from horizontal, 0 = horizontal, 90 = vertical
 *   strike   right-hand rule, 90° counter-clockwise of the dip direction
 */

import type { StereonetPole } from '@/Components/Foundry/Charts';

/** A plane as logged: how steeply it dips, and which way. */
export interface PlaneAttitude {
    true_dip: number | null;
    dip_direction: number | null;
}

/** A measurement that already carries its derived pole. */
export interface DerivedPole {
    strike_deg: number;
    pole_trend_deg: number;
    pole_plunge_deg: number;
}

/** Normalise any bearing into [0, 360). */
export function normaliseBearing(deg: number): number {
    return ((deg % 360) + 360) % 360;
}

/**
 * The pole to a plane: the line perpendicular to it.
 *
 * Trend is opposite the dip direction; plunge is the complement of the
 * dip. A horizontal plane therefore has a vertical pole, which plots at
 * the centre of the net — the check worth remembering, because passing a
 * plane's own dip straight through puts it on the primitive circle
 * instead, where the pole to a *vertical* plane belongs.
 */
export function poleOfPlane(dipDirectionDeg: number, dipDeg: number): StereonetPole {
    return {
        trend_deg: normaliseBearing(dipDirectionDeg + 180),
        plunge_deg: 90 - dipDeg,
    };
}

/** Strike from dip direction, right-hand rule. */
export function strikeOfPlane(dipDirectionDeg: number): number {
    return normaliseBearing(dipDirectionDeg - 90);
}

/**
 * Every plottable pole across both sources.
 *
 * Rows missing either half of the attitude are dropped rather than
 * defaulted: a measurement with a dip and no dip direction is not a
 * measurement at 0° north, and plotting it there would invent a
 * north-dipping population out of incomplete logging.
 */
export function structurePoles(
    planes: readonly PlaneAttitude[],
    derived: readonly DerivedPole[],
): StereonetPole[] {
    const fromPlanes = planes
        .filter((p) => p.true_dip !== null && p.dip_direction !== null)
        .map((p) => poleOfPlane(p.dip_direction as number, p.true_dip as number));

    const fromDerived = derived.map((d) => ({
        trend_deg: normaliseBearing(d.pole_trend_deg),
        plunge_deg: d.pole_plunge_deg,
    }));

    return [...fromPlanes, ...fromDerived];
}

/** Every strike across both sources, for the rose diagram. */
export function structureStrikes(
    planes: readonly PlaneAttitude[],
    derived: readonly DerivedPole[],
): number[] {
    const fromPlanes = planes
        .filter((p) => p.dip_direction !== null)
        .map((p) => strikeOfPlane(p.dip_direction as number));

    const fromDerived = derived.map((d) => normaliseBearing(d.strike_deg));

    return [...fromPlanes, ...fromDerived];
}
