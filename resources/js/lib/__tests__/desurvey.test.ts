import { describe, expect, it } from 'vitest';
import {
    circularMeanDeg,
    deepestIntervalByCollar,
    describeDesurvey,
    desurveyCollars,
    desurveyHole,
    positionAtDepth,
    buildScene3D,
    sceneZAxisTitle,
    worldAtDepth,
    worldPathBetween,
} from '@/lib/desurvey';

const close = (a: number, b: number, tol = 1e-6) => expect(Math.abs(a - b)).toBeLessThan(tol);

describe('circularMeanDeg', () => {
    it('averages across north instead of to 180', () => {
        close(circularMeanDeg([355, 5]) % 360, 0, 1e-9);
        close(circularMeanDeg([350, 20]), 5, 1e-9);
        close(circularMeanDeg([90, 180]), 135, 1e-9);
    });
});

describe('desurveyHole — minimum curvature', () => {
    it('a straight inclined hole lands where trigonometry says', () => {
        // -50° toward 045°, 300 m: 192.8 m horizontal, 229.8 m vertical.
        const h = desurveyHole({ azimuth: 45, dip: -50, totalDepth: 300 }, [
            { depth: 150, azimuth: 45, dip: -50 },
            { depth: 300, azimuth: 45, dip: -50 },
        ]);
        const p = positionAtDepth(h, 300);
        const horiz = 300 * Math.cos((50 * Math.PI) / 180);
        close(p.x, horiz * Math.sin(Math.PI / 4), 1e-6);
        close(p.y, horiz * Math.cos(Math.PI / 4), 1e-6);
        close(p.z, -300 * Math.sin((50 * Math.PI) / 180), 1e-6);
        expect(h.surveyed).toBe(true);
        expect(h.orientation).toBe('surveys');
    });

    it('stations straddling north do not swing the hole south (FE-8)', () => {
        const h = desurveyHole({ azimuth: 355, dip: -60, totalDepth: 200 }, [
            { depth: 100, azimuth: 5, dip: -60 },
            { depth: 200, azimuth: 355, dip: -60 },
        ]);
        for (const pt of h.path) {
            // Northing never goes backwards along a north-trending hole.
            expect(pt.y).toBeGreaterThanOrEqual(-1e-9);
        }
        expect(positionAtDepth(h, 200).y).toBeGreaterThan(90);
        // East/west wobble stays small: nothing like a reversed segment.
        expect(Math.abs(positionAtDepth(h, 200).x)).toBeLessThan(5);
    });

    it('matches the textbook min-curvature step for a curving segment', () => {
        // Build-up from vertical to 30° inclination over 100 m toward north.
        const h = desurveyHole({ azimuth: 0, dip: -90, totalDepth: 100 }, [{ depth: 100, azimuth: 0, dip: -60 }]);
        const beta = (30 * Math.PI) / 180;
        const rf = (2 / beta) * Math.tan(beta / 2);
        const expectedN = (100 / 2) * (0 + Math.cos((60 * Math.PI) / 180)) * rf;
        const expectedZ = -(100 / 2) * (1 + Math.sin((60 * Math.PI) / 180)) * rf;
        const end = positionAtDepth(h, 100);
        close(end.y, expectedN, 1e-6);
        close(end.z, expectedZ, 1e-6);
        // The arc is densified so its curvature is drawn.
        expect(h.path.length).toBeGreaterThan(3);
    });

    it('extends an unsurveyed hole along collar az/dip to TD and flags it (decision 2026-09-29)', () => {
        const h = desurveyHole({ azimuth: 90, dip: -45, totalDepth: 100 }, []);
        expect(h.surveyed).toBe(false);
        expect(h.orientation).toBe('collar');
        expect(h.path).toHaveLength(2);
        const end = h.path[1];
        expect(end.extrapolated).toBe(true);
        close(end.x, 100 * Math.cos(Math.PI / 4));
        close(end.y, 0, 1e-9);
        close(end.z, -100 * Math.sin(Math.PI / 4));
    });

    it('draws a hole with no attitude at all vertically and says so', () => {
        const h = desurveyHole({ azimuth: null, dip: null, totalDepth: 50 }, []);
        expect(h.orientation).toBe('assumed_vertical');
        const end = positionAtDepth(h, 50);
        close(end.x, 0, 1e-9);
        close(end.y, 0, 1e-9);
        close(end.z, -50);
    });

    it('extends beyond the last survey to TD, flagged as extrapolated', () => {
        const h = desurveyHole({ azimuth: 0, dip: -60, totalDepth: 300 }, [{ depth: 100, azimuth: 0, dip: -60 }]);
        expect(h.maxDepth).toBe(300);
        const tail = h.path[h.path.length - 1];
        expect(tail.md).toBe(300);
        expect(tail.extrapolated).toBe(true);
        expect(h.path.filter((p) => p.md <= 100).every((p) => !p.extrapolated)).toBe(true);
    });

    it('reaches the deepest interval even when TD is missing', () => {
        const h = desurveyHole({ azimuth: 0, dip: -90, totalDepth: null }, [], 420);
        expect(h.maxDepth).toBe(420);
        close(positionAtDepth(h, 420).z, -420);
    });

    it('draws a positive dip as an up-hole (§04e 2026-09-29)', () => {
        const up = positionAtDepth(desurveyHole({ azimuth: 0, dip: 60, totalDepth: 10 }, []), 10);
        const down = positionAtDepth(desurveyHole({ azimuth: 0, dip: -60, totalDepth: 10 }, []), 10);
        // Same plan position, mirrored elevation.
        close(up.x, down.x);
        close(up.y, down.y);
        close(up.z, -down.z);
        close(up.z, 10 * Math.sin((60 * Math.PI) / 180));
        expect(up.z).toBeGreaterThan(0);
    });

    it('desurveys a surveyed up-hole upward by minimum curvature', () => {
        // Flattening from +30 to +10 toward east over 100 m: rises, never dips.
        const h = desurveyHole({ azimuth: 90, dip: 30, totalDepth: 100 }, [
            { depth: 0, azimuth: 90, dip: 30 },
            { depth: 100, azimuth: 90, dip: 10 },
        ]);
        for (let i = 1; i < h.path.length; i++) {
            expect(h.path[i].z).toBeGreaterThan(h.path[i - 1].z);
            expect(h.path[i].dip).toBeGreaterThan(0);
        }
        const beta = (20 * Math.PI) / 180;
        const rf = (2 / beta) * Math.tan(beta / 2);
        const expectedZ = (100 / 2) * (Math.sin((30 * Math.PI) / 180) + Math.sin((10 * Math.PI) / 180)) * rf;
        close(positionAtDepth(h, 100).z, expectedZ, 1e-6);
    });

    it('reaches the deepest survey station when TD is missing (§04e 2026-09-29)', () => {
        const h = desurveyHole({ azimuth: 0, dip: -60, totalDepth: null }, [{ depth: 120, azimuth: 0, dip: -60 }]);
        expect(h.maxDepth).toBe(120);
        close(positionAtDepth(h, 120).z, -120 * Math.sin((60 * Math.PI) / 180), 1e-6);
    });
});

describe('placed holes (FE-9)', () => {
    const collars = [
        { collar_id: 'a', easting: 500000, northing: 6000000, elevation: 400, azimuth: 45, dip: -50, total_depth: 400 },
        { collar_id: 'b', easting: null, northing: null, elevation: null, azimuth: 0, dip: -90, total_depth: 100 },
    ];

    it('places an interval at 300 m MD off the collar, not straight beneath it', () => {
        const holes = desurveyCollars(collars, []);
        expect(holes.has('b')).toBe(false); // no position, no hole
        const a = holes.get('a')!;
        const p = worldAtDepth(a, 300);
        const horiz = 300 * Math.cos((50 * Math.PI) / 180);
        close(p.x - 500000, horiz * Math.SQRT1_2, 1e-6);
        close(p.y - 6000000, horiz * Math.SQRT1_2, 1e-6);
        close(p.z, 400 - 300 * Math.sin((50 * Math.PI) / 180), 1e-6);
    });

    it('interval sub-paths start and end at the interval depths', () => {
        const a = desurveyCollars(collars, [{ collar_id: 'a', depth: 200, azimuth: 60, dip: -45 }]).get('a')!;
        const seg = worldPathBetween(a, 150, 250);
        const top = worldAtDepth(a, 150);
        const bottom = worldAtDepth(a, 250);
        close(seg.x[0], top.x);
        close(seg.z[seg.z.length - 1], bottom.z);
        expect(seg.x.length).toBeGreaterThan(2); // passes through the 200 m station
    });

    it('deepestIntervalByCollar and describeDesurvey summarise the set', () => {
        const deepest = deepestIntervalByCollar([
            { collar_id: 'a', to_depth: 50 },
            { collar_id: 'a', to_depth: 480 },
            { collar_id: 'c', depth_m: 12 },
        ]);
        expect(deepest.get('a')).toBe(480);
        expect(deepest.get('c')).toBe(12);
        const holes = desurveyCollars(collars, [], deepest);
        expect(holes.get('a')!.maxDepth).toBe(480);
        expect(describeDesurvey(holes.values())).toContain('1 unsurveyed');
    });
});

describe('z-axis title and terrain heights', () => {
    const base = { easting: 500000, northing: 6000000, azimuth: 0, dip: -90, total_depth: 50 };

    it('plain title when every elevation is a surveyed one', () => {
        const scene = buildScene3D([{ collar_id: 'a', ...base, elevation: 400, elevation_source: 'file' }], []);
        expect(sceneZAxisTitle(scene)).toBe('Elevation (m)');
    });

    it('says so when some heights come from the terrain model, not a survey', () => {
        const scene = buildScene3D(
            [
                { collar_id: 'a', ...base, elevation: 400, elevation_source: 'file' },
                { collar_id: 'b', ...base, elevation: 53.5, elevation_source: 'terrain' },
            ],
            [],
        );
        expect(scene.holes.get('b')!.origin.z).toBe(53.5);
        expect(sceneZAxisTitle(scene)).toBe('Elevation (m · some from terrain model)');
    });

    it('lists both caveats when terrain heights and missing elevations mix', () => {
        const scene = buildScene3D(
            [
                { collar_id: 'b', ...base, elevation: 53.5, elevation_source: 'terrain' },
                { collar_id: 'c', ...base, elevation: null },
            ],
            [],
        );
        expect(sceneZAxisTitle(scene)).toBe('Elevation (m · some from terrain model · collars without one at 0)');
    });
});
