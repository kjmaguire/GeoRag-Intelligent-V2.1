/**
 * Client-side drill-hole desurveying for the Workspace 3D views.
 *
 * ## Why this module exists (FE-8 / FE-9, 2026-09-29 audit)
 *
 * MultiHole3DTrace and OrientationSpiral each carried an average-angle step
 * that took the LINEAR mean of two azimuths — stations at 355° and 5°
 * averaged to 180°, so a north-trending hole drew a segment due south — and
 * both collapsed a hole with no survey rows to a single point, because the
 * loop needs two stations and TD was never passed in. The gold-tier 3D views
 * (intersections, composites, samples, structure discs, lithology) placed
 * every interval at `(collar E, collar N, -measured depth)`: an inclined hole
 * drawn as a vertical stick hung from z = 0.
 *
 * One tested implementation now serves all of them.
 *
 * ## Method
 *
 * Minimum curvature — the same method `promote_silver_to_gold` uses for the
 * server-side traces. Each station's attitude becomes a unit direction vector
 * and each segment is the circular arc between them; because it works on
 * vectors, an azimuth pair straddling north needs no special case.
 * (`circularMeanDeg` is exported for anywhere a single averaged bearing is
 * wanted.)
 *
 * ## Does a hole drawn here agree with its MVT trace? (GIS-12, audit 2026-10)
 *
 * Only as far as the AZIMUTHS agree, and this module does not touch them:
 * WorkspaceController converts a DECLARED reference (a station's own
 * `azimuth_reference`, else the project's `orientation_reference`) to an
 * azimuth from TRUE north before it sends the stations, because true north is
 * this frame's north (`SurveyAzimuthReference`). With that, a hole whose
 * azimuths are true, magnetic (+ the project's declination) or grid north of
 * the project CRS is drawn where its map trace is.
 *
 * Two things are NOT reconciled, and a caller should not claim otherwise:
 *   - a hole with NO declared reference is drawn at its recorded azimuth, read
 *     as true north (Kyle, 2026-09-29: no correction unless declared), while
 *     promote reads the same number as grid north of the collar's UTM zone, so
 *     the map trace differs from this drawing by the grid convergence (about
 *     2.5 degrees at 58 N, 3 degrees from the central meridian);
 *   - a declared reference that could not be applied (magnetic with no
 *     declination, or no usable grid) arrives flagged `azimuth_unapplied`,
 *     drawn as recorded, and the caption says so.
 *
 * Conventions:
 *   - azimuth: degrees clockwise from TRUE north when the server converted a
 *     declared reference, otherwise exactly as recorded (above);
 *   - dip: degrees from horizontal in the silver convention — negative is
 *     below horizontal (-60 = 60° down), positive is an UP-HOLE (+30 = 30°
 *     up). The sign used to be ignored, which drew every up-hole downward;
 *     up-holes are stored as measured since 2026-09-29 (§04e, SME-approved),
 *     so the sign is honoured;
 *   - output: x = east, y = north, z = up, metres relative to the collar;
 *     placed holes are in the scene's local frame (below).
 *
 * ## One frame for every collar (GIS audit 2026-10)
 *
 * `silver.collars.easting/northing` are stored in the CRS and UNITS of
 * whichever upload the collar came from — UTM of any zone, US-survey-foot
 * state plane, lon/lat degrees — so two collars' numbers are not comparable,
 * and adding metre offsets to them is wrong as soon as a project holds more
 * than one upload. They are never used to place a hole here. The position is
 * the collar's EPSG:4326 point (`lng`/`lat`, `silver.collars.geom_4326`),
 * and every hole in a scene is placed in ONE local east/north/up frame of
 * metres about the centroid of the scene's collars (lib/localEnu — the frame
 * DrillTrace3D uses). A collar with no geographic position cannot be placed
 * and is left out, and the caption says how many.
 *
 * ## Holes without surveys
 *
 * Per the 2026-09-29 decision, a hole with no downhole survey rows is drawn
 * along its collar azimuth/dip to TD and flagged `surveyed: false` so the
 * view can mark it. A hole with no collar attitude either is drawn vertical
 * and flagged `orientation: 'assumed_vertical'`. Any length beyond the last
 * station (to TD) is flagged `extrapolated` on the path points.
 */

import { centroidOrigin, toLocalMetres, type LonLatOrigin } from './localEnu';

const DEG = Math.PI / 180;

/** Axis titles for a scene in the local frame: metres about the scene centroid, not a map grid. */
export const EAST_AXIS_TITLE = 'East (m, local)';
export const NORTH_AXIS_TITLE = 'North (m, local)';

export interface SurveyStationInput {
    depth: number;
    azimuth: number | null;
    dip: number | null;
    /**
     * The station declares an azimuth reference (its own, or the project's)
     * that the server could not apply — magnetic with no declination, or no
     * usable grid — so `azimuth` is as recorded.
     */
    azimuth_unapplied?: boolean | null;
}

export interface DesurveyCollar {
    azimuth: number | null;
    dip: number | null;
    /** As SurveyStationInput.azimuth_unapplied, for the collar's own azimuth. */
    azimuthUnapplied?: boolean;
    /**
     * Total depth, or null when the collar has none (§04e 2026-09-29: the
     * column is optional). The path is extended to max(TD, extendTo, last
     * station), so a hole with no TD still reaches its deepest survey
     * station and — through `extendTo` — its deepest interval.
     */
    totalDepth: number | null;
}

export interface PathPoint {
    /** Measured depth along the hole (m). */
    md: number;
    /** East, north, up offsets from the collar (m). */
    x: number;
    y: number;
    z: number;
    azimuth: number;
    dip: number;
    /** True beyond the last attitude measurement (straight-line projection to TD). */
    extrapolated: boolean;
}

export type OrientationSource = 'surveys' | 'collar' | 'assumed_vertical';

export interface DesurveyedHole {
    path: PathPoint[];
    /** At least one downhole survey station contributed. */
    surveyed: boolean;
    orientation: OrientationSource;
    /** A declared azimuth reference of this hole could not be applied: azimuths are as recorded. */
    azimuthUnapplied: boolean;
    /** Deepest MD the path reaches. */
    maxDepth: number;
}

interface Station {
    md: number;
    azimuth: number;
    dip: number;
}

type Vec = [number, number, number];

/**
 * Unit direction vector (east, north, up) for an azimuth / dip.
 * Dip is signed: negative goes down, positive (an up-hole) goes up.
 */
export function directionVector(azimuthDeg: number, dipDeg: number): Vec {
    const a = azimuthDeg * DEG;
    const d = dipDeg * DEG;
    return [Math.cos(d) * Math.sin(a), Math.cos(d) * Math.cos(a), Math.sin(d)];
}

function vecToAttitude(v: Vec): { azimuth: number; dip: number } {
    const horiz = Math.hypot(v[0], v[1]);
    const az = horiz < 1e-12 ? 0 : (Math.atan2(v[0], v[1]) / DEG + 360) % 360;
    // Report dip with the silver sign convention (negative = down).
    return { azimuth: az, dip: -Math.atan2(-v[2], horiz) / DEG };
}

/** Circular mean of bearings in degrees, in [0, 360). */
export function circularMeanDeg(bearings: readonly number[]): number {
    let s = 0;
    let c = 0;
    for (const b of bearings) {
        s += Math.sin(b * DEG);
        c += Math.cos(b * DEG);
    }
    return (Math.atan2(s, c) / DEG + 360) % 360;
}

function dot(a: Vec, b: Vec): number {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function dogleg(a: Vec, b: Vec): number {
    return Math.acos(Math.min(1, Math.max(-1, dot(a, b))));
}

/** Spherical interpolation between unit vectors a and b at fraction f of dogleg beta. */
function slerp(a: Vec, b: Vec, beta: number, f: number): Vec {
    if (beta < 1e-9) return a;
    const sb = Math.sin(beta);
    const wa = Math.sin((1 - f) * beta) / sb;
    const wb = Math.sin(f * beta) / sb;
    return [wa * a[0] + wb * b[0], wa * a[1] + wb * b[1], wa * a[2] + wb * b[2]];
}

/** Minimum-curvature displacement over `dmd` metres from direction a to b. */
function minCurvatureStep(a: Vec, b: Vec, dmd: number): Vec {
    const beta = dogleg(a, b);
    const rf = beta > 1e-9 ? (2 / beta) * Math.tan(beta / 2) : 1;
    const k = (dmd / 2) * rf;
    return [k * (a[0] + b[0]), k * (a[1] + b[1]), k * (a[2] + b[2])];
}

function stationsFor(
    collar: DesurveyCollar,
    surveys: readonly SurveyStationInput[],
): {
    stations: Station[];
    surveyed: boolean;
    orientation: OrientationSource;
} {
    const downhole: Station[] = surveys
        .filter((s) => s.azimuth != null && s.dip != null && Number.isFinite(s.depth) && s.depth >= 0)
        .map((s) => ({ md: s.depth, azimuth: s.azimuth as number, dip: s.dip as number }))
        .sort((a, b) => a.md - b.md);

    // One attitude per depth; the first reading wins.
    const dedup: Station[] = [];
    for (const s of downhole) {
        if (dedup.length === 0 || s.md > dedup[dedup.length - 1].md) dedup.push(s);
    }

    const hasCollarAttitude = collar.azimuth != null && collar.dip != null;
    const stations: Station[] = [];
    if (dedup.length > 0 && dedup[0].md === 0) {
        stations.push(...dedup);
    } else if (hasCollarAttitude) {
        stations.push({ md: 0, azimuth: collar.azimuth as number, dip: collar.dip as number }, ...dedup);
    } else if (dedup.length > 0) {
        // No collar attitude: the shallowest survey stands in at the collar.
        stations.push({ md: 0, azimuth: dedup[0].azimuth, dip: dedup[0].dip }, ...dedup);
    } else {
        stations.push({ md: 0, azimuth: 0, dip: -90 });
    }

    const orientation: OrientationSource =
        dedup.length > 0 ? 'surveys' : hasCollarAttitude ? 'collar' : 'assumed_vertical';

    return { stations, surveyed: dedup.length > 0, orientation };
}

/** Densify an arc segment so its curvature is visible: one point per ~2° of dogleg. */
const ARC_STEP_RAD = 2 * DEG;

/**
 * Desurvey one hole.
 *
 * @param extendTo  Deepest MD any caller needs (e.g. the deepest interval); the
 *                  path always reaches max(TD, extendTo, last station).
 */
export function desurveyHole(
    collar: DesurveyCollar,
    surveys: readonly SurveyStationInput[],
    extendTo = 0,
): DesurveyedHole {
    const { stations, surveyed, orientation } = stationsFor(collar, surveys);
    const lastStationMd = stations[stations.length - 1].md;
    const maxDepth = Math.max(collar.totalDepth ?? 0, extendTo, lastStationMd);

    const path: PathPoint[] = [
        {
            md: 0,
            x: 0,
            y: 0,
            z: 0,
            azimuth: stations[0].azimuth,
            dip: stations[0].dip,
            extrapolated: false,
        },
    ];
    let pos: Vec = [0, 0, 0];

    for (let i = 1; i < stations.length; i++) {
        const a = stations[i - 1];
        const b = stations[i];
        const dmd = b.md - a.md;
        const va = directionVector(a.azimuth, a.dip);
        const vb = directionVector(b.azimuth, b.dip);
        const beta = dogleg(va, vb);
        const n = Math.max(1, Math.ceil(beta / ARC_STEP_RAD));
        const start = pos;
        for (let k = 1; k <= n; k++) {
            const f = k / n;
            const vf = slerp(va, vb, beta, f);
            const d = minCurvatureStep(va, vf, dmd * f);
            const att = k === n ? { azimuth: b.azimuth, dip: b.dip } : vecToAttitude(vf);
            path.push({
                md: a.md + dmd * f,
                x: start[0] + d[0],
                y: start[1] + d[1],
                z: start[2] + d[2],
                azimuth: att.azimuth,
                dip: att.dip,
                extrapolated: false,
            });
        }
        const last = path[path.length - 1];
        pos = [last.x, last.y, last.z];
    }

    if (maxDepth > lastStationMd) {
        const s = stations[stations.length - 1];
        const v = directionVector(s.azimuth, s.dip);
        const len = maxDepth - lastStationMd;
        path.push({
            md: maxDepth,
            x: pos[0] + v[0] * len,
            y: pos[1] + v[1] * len,
            z: pos[2] + v[2] * len,
            azimuth: s.azimuth,
            dip: s.dip,
            extrapolated: true,
        });
    }

    const azimuthUnapplied = collar.azimuthUnapplied === true || surveys.some((st) => st.azimuth_unapplied === true);
    return { path, surveyed, orientation, azimuthUnapplied, maxDepth };
}

/**
 * Collar-relative position at measured depth `md`, interpolated along the
 * path. Depths beyond the path continue along its last direction; negative
 * depths clamp to the collar.
 */
export function positionAtDepth(hole: DesurveyedHole, md: number): { x: number; y: number; z: number } {
    const p = hole.path;
    if (p.length === 1 || md <= 0) return { x: p[0].x, y: p[0].y, z: p[0].z };
    let i = 1;
    while (i < p.length - 1 && p[i].md < md) i++;
    const a = p[i - 1];
    const b = p[i];
    const span = b.md - a.md;
    const f = span > 0 ? (md - a.md) / span : 0;
    return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f, z: a.z + (b.z - a.z) * f };
}

/**
 * The sub-path between two measured depths (inclusive), for drawing an
 * interval as a segment of the real hole rather than a vertical stick.
 */
export function pathBetween(
    hole: DesurveyedHole,
    fromMd: number,
    toMd: number,
): { x: number[]; y: number[]; z: number[] } {
    const lo = Math.min(fromMd, toMd);
    const hi = Math.max(fromMd, toMd);
    const out = { x: [] as number[], y: [] as number[], z: [] as number[] };
    const push = (q: { x: number; y: number; z: number }) => {
        out.x.push(q.x);
        out.y.push(q.y);
        out.z.push(q.z);
    };
    push(positionAtDepth(hole, lo));
    for (const pt of hole.path) {
        if (pt.md > lo && pt.md < hi) push(pt);
    }
    push(positionAtDepth(hole, hi));
    return out;
}

export interface CollarForDesurvey {
    collar_id: string;
    /**
     * The collar's EPSG:4326 position (`silver.collars.geom_4326`) — the one
     * position that means the same thing for every collar. A collar without it
     * is not placed.
     */
    lng?: number | null;
    lat?: number | null;
    /**
     * As stored: in the CRS and units of the collar's own upload (UTM of any
     * zone, US-ft state plane, degrees). Never used to place a hole; kept so a
     * view can show the stored value.
     */
    easting?: number | null;
    northing?: number | null;
    elevation?: number | null;
    /** 'terrain': `elevation` is a terrain-model ground height, not a surveyed RL. */
    elevation_source?: 'file' | 'terrain' | null;
    azimuth?: number | null;
    dip?: number | null;
    /** As SurveyStationInput.azimuth_unapplied, for the collar's own azimuth. */
    azimuth_unapplied?: boolean | null;
    total_depth?: number | null;
}

/** East/north metres about a lon/lat origin: the one frame a scene is drawn in. */
export interface LocalFrame {
    origin: LonLatOrigin;
    toLocal: (lng: number, lat: number) => { east: number; north: number };
}

/** True when the collar has the EPSG:4326 position it needs to be placed. */
export function hasLonLat(c: { lng?: number | null; lat?: number | null }): boolean {
    return c.lng != null && c.lat != null && Number.isFinite(c.lng) && Number.isFinite(c.lat);
}

/** The local frame about the centroid of the collars that have a position; null when none do. */
export function localFrameFor(collars: ReadonlyArray<{ lng?: number | null; lat?: number | null }>): LocalFrame | null {
    const origin = centroidOrigin(
        collars.filter(hasLonLat).map((c) => ({ lon: c.lng as number, lat: c.lat as number })),
    );
    return origin ? { origin, toLocal: toLocalMetres(origin) } : null;
}

export interface PlacedHole extends DesurveyedHole {
    collar_id: string;
    /**
     * Collar position in the scene's local frame: x = metres east and
     * y = metres north of the frame origin, z = elevation (0 when unknown).
     */
    origin: { x: number; y: number; z: number };
    elevationKnown: boolean;
    /** The z is a terrain-model height (a surface model: it reads canopy in forest). */
    elevationFromTerrain: boolean;
}

/**
 * Desurvey every collar that has a position and place it in `frame` (default:
 * the local frame about these collars' own centroid). `extendTo` lets a view
 * make sure each hole reaches its deepest interval even when TD is missing or
 * short. A collar with no lng/lat is not placed.
 */
export function desurveyCollars(
    collars: readonly CollarForDesurvey[],
    surveys: ReadonlyArray<SurveyStationInput & { collar_id: string }>,
    extendTo: ReadonlyMap<string, number> = new Map(),
    frame: LocalFrame | null = localFrameFor(collars),
): Map<string, PlacedHole> {
    const byCollar = new Map<string, SurveyStationInput[]>();
    for (const s of surveys) {
        const list = byCollar.get(s.collar_id);
        if (list) list.push(s);
        else byCollar.set(s.collar_id, [s]);
    }
    const out = new Map<string, PlacedHole>();
    if (!frame) return out;
    for (const c of collars) {
        if (!hasLonLat(c)) continue;
        const hole = desurveyHole(
            {
                azimuth: c.azimuth ?? null,
                dip: c.dip ?? null,
                totalDepth: c.total_depth ?? null,
                azimuthUnapplied: c.azimuth_unapplied === true,
            },
            byCollar.get(c.collar_id) ?? [],
            extendTo.get(c.collar_id) ?? 0,
        );
        const at = frame.toLocal(c.lng as number, c.lat as number);
        out.set(c.collar_id, {
            ...hole,
            collar_id: c.collar_id,
            origin: { x: at.east, y: at.north, z: c.elevation ?? 0 },
            elevationKnown: c.elevation != null,
            elevationFromTerrain: c.elevation != null && c.elevation_source === 'terrain',
        });
    }
    return out;
}

/** Local-frame (east, north, elevation) of MD `md` on a placed hole. */
export function worldAtDepth(hole: PlacedHole, md: number): { x: number; y: number; z: number } {
    const p = positionAtDepth(hole, md);
    return { x: hole.origin.x + p.x, y: hole.origin.y + p.y, z: hole.origin.z + p.z };
}

/** Local-frame sub-path between two MDs on a placed hole. */
export function worldPathBetween(
    hole: PlacedHole,
    fromMd: number,
    toMd: number,
): { x: number[]; y: number[]; z: number[] } {
    const p = pathBetween(hole, fromMd, toMd);
    return {
        x: p.x.map((v) => v + hole.origin.x),
        y: p.y.map((v) => v + hole.origin.y),
        z: p.z.map((v) => v + hole.origin.z),
    };
}

/** Deepest `to` depth per collar across interval-like rows. */
export function deepestIntervalByCollar(
    rows: ReadonlyArray<{ collar_id: string; to_depth?: number | null; depth_m?: number | null }>,
): Map<string, number> {
    const out = new Map<string, number>();
    for (const r of rows) {
        const d = r.to_depth ?? r.depth_m ?? null;
        if (d == null || !Number.isFinite(d)) continue;
        out.set(r.collar_id, Math.max(out.get(r.collar_id) ?? 0, d));
    }
    return out;
}

export interface XYZ {
    x: number;
    y: number;
    z: number;
}
export interface XYZArrays {
    x: number[];
    y: number[];
    z: number[];
}

/**
 * A desurveyed set of holes in one Plotly scene, in the LOCAL frame: x = metres
 * east and y = metres north of the centroid of the placed collars (taken from
 * their lng/lat, never from the stored easting/northing, which are in each
 * upload's own CRS), z = collar elevation + desurveyed offset (collars without
 * an elevation start at 0 — `elevationKnownForAll` says whether that
 * happened).
 *
 * Replaces the `(collar E, collar N, -measured depth)` placement the gold 3D
 * views used, which hung every inclined hole vertically from z = 0 (FE-9).
 */
export interface Scene3D {
    holes: Map<string, PlacedHole>;
    /** The frame every hole is placed in; null when no collar had a position. */
    frame: LocalFrame | null;
    /** Collars left out for want of a lng/lat. */
    unplaced: number;
    at(collarId: string, md: number): XYZ | null;
    segment(collarId: string, fromMd: number, toMd: number): XYZArrays | null;
    fullPath(collarId: string): XYZArrays | null;
    caption: string;
    elevationKnownForAll: boolean;
    elevationFromTerrainAny: boolean;
}

/**
 * @param notPlaced  Collars the caller already left out because they have no
 *                   lng/lat (views pre-filter), so the caption can say so.
 */
export function buildScene3D(
    collars: readonly CollarForDesurvey[],
    surveys: ReadonlyArray<SurveyStationInput & { collar_id: string }>,
    extendTo: ReadonlyMap<string, number> = new Map(),
    notPlaced = 0,
): Scene3D {
    const frame = localFrameFor(collars);
    const holes = desurveyCollars(collars, surveys, extendTo, frame);
    const unplaced = notPlaced + collars.filter((c) => !hasLonLat(c)).length;
    // Hole origins are already local metres: no re-centring on raw numbers.
    const shift = (p: XYZ, h: PlacedHole): XYZ => ({
        x: h.origin.x + p.x,
        y: h.origin.y + p.y,
        z: h.origin.z + p.z,
    });
    const shiftArrays = (a: { x: number[]; y: number[]; z: number[] }, h: PlacedHole): XYZArrays => ({
        x: a.x.map((v) => v + h.origin.x),
        y: a.y.map((v) => v + h.origin.y),
        z: a.z.map((v) => v + h.origin.z),
    });

    return {
        holes,
        frame,
        unplaced,
        at(collarId, md) {
            const h = holes.get(collarId);
            return h ? shift(positionAtDepth(h, md), h) : null;
        },
        segment(collarId, fromMd, toMd) {
            const h = holes.get(collarId);
            return h ? shiftArrays(pathBetween(h, fromMd, toMd), h) : null;
        },
        fullPath(collarId) {
            const h = holes.get(collarId);
            if (!h) return null;
            return shiftArrays(
                {
                    x: h.path.map((p) => p.x),
                    y: h.path.map((p) => p.y),
                    z: h.path.map((p) => p.z),
                },
                h,
            );
        },
        caption: describeDesurvey(holes.values(), { unplaced, frame }),
        elevationKnownForAll: Array.from(holes.values()).every((h) => h.elevationKnown),
        elevationFromTerrainAny: Array.from(holes.values()).some((h) => h.elevationFromTerrain),
    };
}

/**
 * Plotly scene aspect for every 3D view built on a Scene3D: true scale.
 *
 * The scene is in metres on all three axes (easting and northing offsets from
 * the project centroid, elevation), and `aspectmode: 'data'` draws 1 m the same
 * length on each, so a hole's apparent dip and azimuth can be read off the plot.
 * The five Workspace views used `aspectmode: 'manual'` with `{ x: 1, y: 1,
 * z: 0.6 }`, which fits whatever the data spans into a fixed 1 : 1 : 0.6 box:
 * a 600 m x 200 m footprint came out square, and every dip was off by an amount
 * that depended on the project's shape. DrillTrace3D, MultiHole3DTrace and
 * OrientationSpiral already draw true scale (GIS-9, 2026-09-29); this is the
 * same rule for the rest. A project that is much deeper than it is wide draws as
 * a tall box, which is what it is; the camera can be rotated and zoomed.
 */
export const SCENE_3D_ASPECT = { aspectmode: 'data' } as const;

/** z-axis title for a Scene3D. */
export function sceneZAxisTitle(
    scene: Pick<Scene3D, 'elevationKnownForAll'> & Partial<Pick<Scene3D, 'elevationFromTerrainAny'>>,
): string {
    const notes: string[] = [];
    if (scene.elevationFromTerrainAny) notes.push('some from terrain model');
    if (!scene.elevationKnownForAll) notes.push('collars without one at 0');
    return notes.length ? `Elevation (m · ${notes.join(' · ')})` : 'Elevation (m)';
}

/** "58.2468°N 106.1234°W" — where a local frame is centred. */
export function describeOrigin(origin: LonLatOrigin): string {
    const lat = `${Math.abs(origin.lat).toFixed(4)}°${origin.lat >= 0 ? 'N' : 'S'}`;
    const lon = `${Math.abs(origin.lon).toFixed(4)}°${origin.lon >= 0 ? 'E' : 'W'}`;
    return `${lat} ${lon}`;
}

/**
 * One-line caption for a set of placed holes, so a view can say how many are
 * drawn from real surveys and how many are projections, how many could not be
 * placed, and where the local frame is centred.
 */
export function describeDesurvey(
    holes: Iterable<PlacedHole>,
    opts: { unplaced?: number; frame?: LocalFrame | null } = {},
): string {
    let surveyed = 0;
    let collarOnly = 0;
    let vertical = 0;
    let unapplied = 0;
    for (const h of holes) {
        if (h.orientation === 'surveys') surveyed++;
        else if (h.orientation === 'collar') collarOnly++;
        else vertical++;
        if (h.azimuthUnapplied) unapplied++;
    }
    const parts = [`${surveyed} desurveyed (minimum curvature)`];
    if (collarOnly > 0) parts.push(`${collarOnly} unsurveyed — projected along collar azimuth/dip`);
    if (vertical > 0) parts.push(`${vertical} with no orientation — drawn vertical`);
    if (unapplied > 0) {
        parts.push(`${unapplied} with a declared azimuth reference that could not be applied — azimuths as recorded`);
    }
    if ((opts.unplaced ?? 0) > 0) parts.push(`${opts.unplaced} with no geographic position — not drawn`);
    if (opts.frame) parts.push(`metres about ${describeOrigin(opts.frame.origin)}`);
    return parts.join(' · ');
}
