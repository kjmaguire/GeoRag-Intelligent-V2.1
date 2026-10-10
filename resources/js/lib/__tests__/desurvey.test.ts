import { describe, expect, it } from 'vitest';
import {
    circularMeanDeg,
    deepestIntervalByCollar,
    describeDesurvey,
    desurveyCollars,
    desurveyHole,
    positionAtDepth,
    buildScene3D,
    hasLonLat,
    localFrameFor,
    sceneZAxisTitle,
    worldAtDepth,
    worldPathBetween,
    EAST_AXIS_TITLE,
    NORTH_AXIS_TITLE,
} from '@/lib/desurvey';
import { metresPerDegree } from '@/lib/localEnu';

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
    // Collar 'a' is the only one with a position, so it is the frame origin.
    const collars = [
        {
            collar_id: 'a',
            lng: -105,
            lat: 58,
            easting: 500000,
            northing: 6000000,
            elevation: 400,
            azimuth: 45,
            dip: -50,
            total_depth: 400,
        },
        { collar_id: 'b', lng: null, lat: null, elevation: null, azimuth: 0, dip: -90, total_depth: 100 },
    ];

    it('places an interval at 300 m MD off the collar, not straight beneath it', () => {
        const holes = desurveyCollars(collars, []);
        expect(holes.has('b')).toBe(false); // no position, no hole
        const a = holes.get('a')!;
        const p = worldAtDepth(a, 300);
        const horiz = 300 * Math.cos((50 * Math.PI) / 180);
        // The frame origin is the collar itself, so east/north are the offsets.
        close(p.x, horiz * Math.SQRT1_2, 1e-6);
        close(p.y, horiz * Math.SQRT1_2, 1e-6);
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
    const base = { lng: -105, lat: 58, easting: 500000, northing: 6000000, azimuth: 0, dip: -90, total_depth: 50 };

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

// ── One frame for every collar (GIS audit 2026-10, finding 3) ───────────────
//
// silver.collars.easting/northing are stored in the CRS and UNITS of whichever
// upload the collar came from. The 3D views added metre offsets to those raw
// numbers and labelled the axis "Easting (m)", so a project with two uploads
// (UTM metres and US-ft state plane, say) drew its holes millions of "metres"
// apart. The position is the collar's EPSG:4326 point.
describe('one local frame for every collar (GIS audit 2026-10)', () => {
    // The same neighbourhood, stored three ways. lng/lat is consistent; the
    // easting/northing are in three different systems and mean nothing together.
    const mixedUploads = [
        // UTM 13N, metres.
        { collar_id: 'utm', lng: -105.0, lat: 58.0, easting: 500000, northing: 6427000, azimuth: 0, dip: -90 },
        // US-survey-foot state plane.
        { collar_id: 'ft', lng: -104.99, lat: 58.0, easting: 2100000, northing: 600000, azimuth: 0, dip: -90 },
        // Geographic degrees.
        { collar_id: 'deg', lng: -104.98, lat: 58.0, easting: -104.98, northing: 58.0, azimuth: 0, dip: -90 },
    ];

    it('places collars by lng/lat, so two uploads in different CRSs land where they really are', () => {
        const holes = desurveyCollars(mixedUploads, []);
        const east = (id: string) => holes.get(id)!.origin.x;
        const north = (id: string) => holes.get(id)!.origin.y;
        const perDegLon = metresPerDegree(58).lon;

        // 0.01 degrees of longitude at 58 N is ~591 m.
        close(east('ft') - east('utm'), 0.01 * perDegLon, 1e-6);
        close(east('deg') - east('utm'), 0.02 * perDegLon, 1e-6);
        close(north('ft') - north('utm'), 0, 1e-6);
        close(north('deg') - north('utm'), 0, 1e-6);
    });

    it('does not read the stored easting/northing at all', () => {
        const scrambled = mixedUploads.map((c) => ({ ...c, easting: 1e9, northing: -1e9 }));
        const a = desurveyCollars(mixedUploads, []);
        const b = desurveyCollars(scrambled, []);
        for (const id of ['utm', 'ft', 'deg']) {
            expect(b.get(id)!.origin).toEqual(a.get(id)!.origin);
        }
    });

    it('keeps every hole within a few km of the frame origin however its easting is stored', () => {
        const scene = buildScene3D(mixedUploads, []);
        for (const h of scene.holes.values()) {
            expect(Math.hypot(h.origin.x, h.origin.y)).toBeLessThan(5000);
        }
    });

    it('a scene point is the local offset plus the desurveyed offset, in that one frame', () => {
        const scene = buildScene3D(
            [
                {
                    collar_id: 'a',
                    lng: -105.0,
                    lat: 58.0,
                    easting: 500000,
                    northing: 6427000,
                    elevation: 300,
                    azimuth: 90,
                    dip: -60,
                    total_depth: 100,
                },
                {
                    collar_id: 'b',
                    lng: -104.99,
                    lat: 58.0,
                    easting: 2100000,
                    northing: 600000,
                    elevation: 300,
                    azimuth: 90,
                    dip: -60,
                    total_depth: 100,
                },
            ],
            [],
        );
        const a = scene.at('a', 100)!;
        const b = scene.at('b', 100)!;
        // Same attitude and depth: the two toes differ by exactly the collar separation.
        close(b.x - a.x, 0.01 * metresPerDegree(58).lon, 1e-6);
        close(b.z, a.z, 1e-9);
    });

    it('leaves out a collar with no lng/lat and says so, instead of drawing it at its raw easting', () => {
        const scene = buildScene3D(
            [
                ...mixedUploads,
                {
                    collar_id: 'noLonLat',
                    lng: null,
                    lat: null,
                    easting: 500123,
                    northing: 6427123,
                    azimuth: 0,
                    dip: -90,
                },
            ],
            [],
        );
        expect(scene.holes.has('noLonLat')).toBe(false);
        expect(scene.unplaced).toBe(1);
        expect(scene.caption).toContain('1 with no geographic position — not drawn');
    });

    it('counts holes a view already filtered out for want of a position', () => {
        const scene = buildScene3D(mixedUploads, [], new Map(), 2);
        expect(scene.unplaced).toBe(2);
        expect(scene.caption).toContain('2 with no geographic position');
    });

    it('names where the frame is centred', () => {
        const scene = buildScene3D(mixedUploads, []);
        expect(scene.frame).not.toBeNull();
        expect(scene.caption).toMatch(/metres about 58\.0000°N 104\.9900°W/);
    });

    it('has no frame, and places nothing, when no collar has a position', () => {
        const scene = buildScene3D([{ collar_id: 'x', easting: 1, northing: 2 }], []);
        expect(scene.frame).toBeNull();
        expect(scene.holes.size).toBe(0);
        expect(scene.unplaced).toBe(1);
    });

    it('rejects non-finite coordinates', () => {
        expect(hasLonLat({ lng: NaN, lat: 58 })).toBe(false);
        expect(hasLonLat({ lng: -105, lat: null })).toBe(false);
        expect(hasLonLat({ lng: -105, lat: 58 })).toBe(true);
        expect(localFrameFor([{ lng: NaN, lat: 58 }])).toBeNull();
    });

    it('labels the axes as local metres, not as a map grid', () => {
        expect(EAST_AXIS_TITLE).toBe('East (m, local)');
        expect(NORTH_AXIS_TITLE).toBe('North (m, local)');
    });

    it('keeps a project that straddles the antimeridian together (about 14 km, not 20,000 km)', () => {
        const holes = desurveyCollars(
            [
                { collar_id: 'w', lng: 179.9, lat: 50, azimuth: 0, dip: -90 },
                { collar_id: 'e', lng: -179.9, lat: 50, azimuth: 0, dip: -90 },
            ],
            [],
        );
        const gap = holes.get('e')!.origin.x - holes.get('w')!.origin.x;
        expect(gap).toBeGreaterThan(14000);
        expect(gap).toBeLessThan(14600);
    });
});

// ── Declared azimuth references (GIS audit 2026-10, finding 5) ───────────────
//
// WorkspaceController converts a declared reference (a station's own, else the
// project's) to an azimuth from true north before it sends the stations. This
// module only has to carry through what the server could NOT convert, so the
// caption can say so instead of drawing it as if it were corrected.
describe('azimuth references the server could not apply (GIS-12)', () => {
    const collar = { collar_id: 'a', lng: -102, lat: 58, azimuth: 90, dip: -60, total_depth: 100 };

    it('flags a hole whose stations carry azimuth_unapplied, and the caption says so', () => {
        const holes = desurveyCollars(
            [collar],
            [
                { collar_id: 'a', depth: 0, azimuth: 90, dip: -60, azimuth_unapplied: true },
                { collar_id: 'a', depth: 50, azimuth: 92, dip: -60, azimuth_unapplied: true },
            ],
        );
        expect(holes.get('a')!.azimuthUnapplied).toBe(true);
        expect(describeDesurvey(holes.values())).toContain(
            '1 with a declared azimuth reference that could not be applied — azimuths as recorded',
        );
    });

    it('flags a hole whose own collar azimuth could not be converted', () => {
        const holes = desurveyCollars([{ ...collar, azimuth_unapplied: true }], []);
        expect(holes.get('a')!.azimuthUnapplied).toBe(true);
    });

    it('does not flag, or mention, an ordinary hole', () => {
        const holes = desurveyCollars([collar], [{ collar_id: 'a', depth: 0, azimuth: 90, dip: -60 }]);
        expect(holes.get('a')!.azimuthUnapplied).toBe(false);
        expect(describeDesurvey(holes.values())).not.toContain('azimuth reference');
    });

    it('draws a flagged station at the azimuth it was given: converting is the servers job', () => {
        const unflagged = desurveyHole({ azimuth: 90, dip: -60, totalDepth: 100 }, [
            { depth: 100, azimuth: 90, dip: -60 },
        ]);
        const flagged = desurveyHole({ azimuth: 90, dip: -60, totalDepth: 100 }, [
            { depth: 100, azimuth: 90, dip: -60, azimuth_unapplied: true },
        ]);
        expect(flagged.path).toEqual(unflagged.path);
        expect(flagged.azimuthUnapplied).toBe(true);
    });

    it('carries the flag into a scene caption', () => {
        const scene = buildScene3D([{ ...collar, azimuth_unapplied: true }], []);
        expect(scene.caption).toContain('could not be applied');
    });
});
