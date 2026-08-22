/**
 * Pole and strike conversion.
 *
 * These assertions are geometry, not implementation detail: a stereonet
 * that is ninety degrees out still draws a plausible-looking cluster, so
 * the failure mode is a confidently wrong structural interpretation
 * rather than a blank panel. The canonical reference is
 * Components/HoleAnalysis/Stereonet — `poleOfPlane` there and here must
 * agree, because both nets appear on the same page.
 */
import { describe, it, expect } from 'vitest';

import {
    normaliseBearing,
    poleOfPlane,
    strikeOfPlane,
    structurePoles,
    structureStrikes,
    type DerivedPole,
    type PlaneAttitude,
} from '../structureProjection';

describe('normaliseBearing', () => {
    it.each([
        [0, 0],
        [359, 359],
        [360, 0],
        [450, 90],
        [-90, 270],
        [-1, 359],
        [720, 0],
    ])('%d° → %d°', (input, expected) => {
        expect(normaliseBearing(input)).toBe(expected);
    });
});

describe('poleOfPlane', () => {
    it('a horizontal plane has a vertical pole', () => {
        // The check worth remembering: plunge 90 projects to the centre of
        // the net. Passing the plane's own dip through instead would put
        // this on the primitive circle, where the pole to a VERTICAL plane
        // belongs — the exact error the old prop name invited.
        expect(poleOfPlane(0, 0)).toEqual({ trend_deg: 180, plunge_deg: 90 });
    });

    it('a vertical plane has a horizontal pole', () => {
        expect(poleOfPlane(90, 90)).toEqual({ trend_deg: 270, plunge_deg: 0 });
    });

    it('trend is opposite the dip direction', () => {
        expect(poleOfPlane(45, 30).trend_deg).toBe(225);
        expect(poleOfPlane(200, 30).trend_deg).toBe(20);
    });

    it('plunge is the complement of the dip', () => {
        expect(poleOfPlane(0, 30).plunge_deg).toBe(60);
        expect(poleOfPlane(0, 60).plunge_deg).toBe(30);
    });

    it('a bed dipping 30° east has its pole plunging 60° west', () => {
        // A real reading, checked end to end: dip direction 090, dip 30.
        expect(poleOfPlane(90, 30)).toEqual({ trend_deg: 270, plunge_deg: 60 });
    });
});

describe('strikeOfPlane', () => {
    it('is 90° counter-clockwise of the dip direction', () => {
        expect(strikeOfPlane(90)).toBe(0);
        expect(strikeOfPlane(180)).toBe(90);
    });

    it('wraps below north rather than going negative', () => {
        expect(strikeOfPlane(0)).toBe(270);
        expect(strikeOfPlane(45)).toBe(315);
    });
});

describe('structurePoles', () => {
    const plane = (over: Partial<PlaneAttitude> = {}): PlaneAttitude => ({
        true_dip: 30,
        dip_direction: 90,
        ...over,
    });
    const derived = (over: Partial<DerivedPole> = {}): DerivedPole => ({
        strike_deg: 45,
        pole_trend_deg: 315,
        pole_plunge_deg: 20,
        ...over,
    });

    it('converts logged plane attitudes', () => {
        expect(structurePoles([plane()], [])).toEqual([
            { trend_deg: 270, plunge_deg: 60 },
        ]);
    });

    it('passes an already-derived pole through untouched', () => {
        expect(structurePoles([], [derived()])).toEqual([
            { trend_deg: 315, plunge_deg: 20 },
        ]);
    });

    it('uses both sources — a project can have either or both', () => {
        expect(structurePoles([plane()], [derived()])).toHaveLength(2);
    });

    it('drops a reading with no dip direction instead of defaulting it', () => {
        // Defaulting to 0 would invent a north-dipping population out of
        // incomplete logging, and it would look like a real result.
        expect(structurePoles([plane({ dip_direction: null })], [])).toEqual([]);
    });

    it('drops a reading with no dip', () => {
        expect(structurePoles([plane({ true_dip: null })], [])).toEqual([]);
    });

    it('keeps the complete readings when only some are partial', () => {
        const rows = [plane(), plane({ true_dip: null }), plane({ dip_direction: 180 })];
        expect(structurePoles(rows, [])).toHaveLength(2);
    });

    it('is empty for empty input', () => {
        expect(structurePoles([], [])).toEqual([]);
    });
});

describe('structureStrikes', () => {
    it('derives strike from the dip direction', () => {
        expect(structureStrikes([{ true_dip: 30, dip_direction: 90 }], [])).toEqual([0]);
    });

    it('takes the stored strike from the gold source', () => {
        expect(structureStrikes([], [
            { strike_deg: 45, pole_trend_deg: 315, pole_plunge_deg: 20 },
        ])).toEqual([45]);
    });

    it('needs only a dip direction, not a dip', () => {
        // A strike is a compass bearing; it does not care how steeply the
        // plane dips. Requiring both would silently thin the rose diagram.
        expect(structureStrikes([{ true_dip: null, dip_direction: 180 }], [])).toEqual([90]);
    });

    it('drops a reading with no dip direction', () => {
        expect(structureStrikes([{ true_dip: 30, dip_direction: null }], [])).toEqual([]);
    });

    it('normalises a stored strike that is out of range', () => {
        expect(structureStrikes([], [
            { strike_deg: 400, pole_trend_deg: 0, pole_plunge_deg: 0 },
        ])).toEqual([40]);
    });

    it('every strike lands in a rose bin', () => {
        // RoseMini does Math.floor((s % 360) / 360 * 36); a negative bearing
        // would index -1 and drop the reading silently.
        const strikes = structureStrikes(
            [
                { true_dip: 10, dip_direction: 0 },
                { true_dip: 10, dip_direction: 45 },
                { true_dip: 10, dip_direction: 359 },
            ],
            [{ strike_deg: -30, pole_trend_deg: 0, pole_plunge_deg: 0 }],
        );

        for (const s of strikes) {
            const bin = Math.floor(((s % 360) / 360) * 36);
            expect(bin).toBeGreaterThanOrEqual(0);
            expect(bin).toBeLessThan(36);
        }
    });
});
