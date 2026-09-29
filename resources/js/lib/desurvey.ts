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
 * server-side traces, so a hole drawn here agrees with its MVT trace on the
 * map. Each station's attitude becomes a unit direction vector and each
 * segment is the circular arc between them; because it works on vectors, an
 * azimuth pair straddling north needs no special case. (`circularMeanDeg` is
 * exported for anywhere a single averaged bearing is wanted.)
 *
 * Conventions:
 *   - azimuth: degrees clockwise from north, in whatever north the data uses
 *     (declination / grid convergence are NOT applied — see GIS-12);
 *   - dip: inclination below horizontal; the sign is ignored (-60 and 60 are
 *     both "60° down"), matching the previous behaviour and the silver data;
 *   - output: x = east, y = north, z = up, metres relative to the collar.
 *
 * ## Holes without surveys
 *
 * Per the 2026-09-29 decision, a hole with no downhole survey rows is drawn
 * along its collar azimuth/dip to TD and flagged `surveyed: false` so the
 * view can mark it. A hole with no collar attitude either is drawn vertical
 * and flagged `orientation: 'assumed_vertical'`. Any length beyond the last
 * station (to TD) is flagged `extrapolated` on the path points.
 */

const DEG = Math.PI / 180;

export interface SurveyStationInput {
    depth: number;
    azimuth: number | null;
    dip: number | null;
}

export interface DesurveyCollar {
    azimuth: number | null;
    dip: number | null;
    /** Total depth; the path is extended to max(TD, extendTo). */
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
    /** Deepest MD the path reaches. */
    maxDepth: number;
}

interface Station {
    md: number;
    azimuth: number;
    dip: number;
}

type Vec = [number, number, number];

/** Unit direction vector (east, north, up) for an azimuth / dip. */
export function directionVector(azimuthDeg: number, dipDeg: number): Vec {
    const a = azimuthDeg * DEG;
    const d = Math.abs(dipDeg) * DEG;
    return [Math.cos(d) * Math.sin(a), Math.cos(d) * Math.cos(a), -Math.sin(d)];
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
    return ((Math.atan2(s, c) / DEG) + 360) % 360;
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

function stationsFor(collar: DesurveyCollar, surveys: readonly SurveyStationInput[]): {
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

    const orientation: OrientationSource = dedup.length > 0
        ? 'surveys'
        : hasCollarAttitude ? 'collar' : 'assumed_vertical';

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

    const path: PathPoint[] = [{
        md: 0, x: 0, y: 0, z: 0,
        azimuth: stations[0].azimuth, dip: stations[0].dip,
        extrapolated: false,
    }];
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
                x: start[0] + d[0], y: start[1] + d[1], z: start[2] + d[2],
                azimuth: att.azimuth, dip: att.dip,
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
            x: pos[0] + v[0] * len, y: pos[1] + v[1] * len, z: pos[2] + v[2] * len,
            azimuth: s.azimuth, dip: s.dip,
            extrapolated: true,
        });
    }

    return { path, surveyed, orientation, maxDepth };
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
export function pathBetween(hole: DesurveyedHole, fromMd: number, toMd: number): { x: number[]; y: number[]; z: number[] } {
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
    easting: number | null;
    northing: number | null;
    elevation?: number | null;
    azimuth?: number | null;
    dip?: number | null;
    total_depth?: number | null;
}

export interface PlacedHole extends DesurveyedHole {
    collar_id: string;
    /** Absolute collar position: easting, northing, elevation (0 when unknown). */
    origin: { x: number; y: number; z: number };
    elevationKnown: boolean;
}

/**
 * Desurvey every collar that has a position. `extendTo` lets a view make sure
 * each hole reaches its deepest interval even when TD is missing or short.
 */
export function desurveyCollars(
    collars: readonly CollarForDesurvey[],
    surveys: ReadonlyArray<SurveyStationInput & { collar_id: string }>,
    extendTo: ReadonlyMap<string, number> = new Map(),
): Map<string, PlacedHole> {
    const byCollar = new Map<string, SurveyStationInput[]>();
    for (const s of surveys) {
        const list = byCollar.get(s.collar_id);
        if (list) list.push(s);
        else byCollar.set(s.collar_id, [s]);
    }
    const out = new Map<string, PlacedHole>();
    for (const c of collars) {
        if (c.easting == null || c.northing == null) continue;
        const hole = desurveyHole(
            { azimuth: c.azimuth ?? null, dip: c.dip ?? null, totalDepth: c.total_depth ?? null },
            byCollar.get(c.collar_id) ?? [],
            extendTo.get(c.collar_id) ?? 0,
        );
        out.set(c.collar_id, {
            ...hole,
            collar_id: c.collar_id,
            origin: { x: c.easting, y: c.northing, z: c.elevation ?? 0 },
            elevationKnown: c.elevation != null,
        });
    }
    return out;
}

/** Absolute (easting, northing, elevation) of MD `md` on a placed hole. */
export function worldAtDepth(hole: PlacedHole, md: number): { x: number; y: number; z: number } {
    const p = positionAtDepth(hole, md);
    return { x: hole.origin.x + p.x, y: hole.origin.y + p.y, z: hole.origin.z + p.z };
}

/** Absolute sub-path between two MDs on a placed hole. */
export function worldPathBetween(hole: PlacedHole, fromMd: number, toMd: number): { x: number[]; y: number[]; z: number[] } {
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

export interface XYZ { x: number; y: number; z: number }
export interface XYZArrays { x: number[]; y: number[]; z: number[] }

/**
 * A desurveyed set of holes in one Plotly scene: easting/northing re-centred
 * on the mean collar (so the axes read in tens/hundreds of metres, not
 * 6,000,000), z = collar elevation + desurveyed offset (collars without an
 * elevation start at 0 — `elevationKnownForAll` says whether that happened).
 *
 * Replaces the `(collar E, collar N, -measured depth)` placement the gold 3D
 * views used, which hung every inclined hole vertically from z = 0 (FE-9).
 */
export interface Scene3D {
    holes: Map<string, PlacedHole>;
    at(collarId: string, md: number): XYZ | null;
    segment(collarId: string, fromMd: number, toMd: number): XYZArrays | null;
    fullPath(collarId: string): XYZArrays | null;
    caption: string;
    elevationKnownForAll: boolean;
}

export function buildScene3D(
    collars: readonly CollarForDesurvey[],
    surveys: ReadonlyArray<SurveyStationInput & { collar_id: string }>,
    extendTo: ReadonlyMap<string, number> = new Map(),
): Scene3D {
    const holes = desurveyCollars(collars, surveys, extendTo);
    let sumE = 0;
    let sumN = 0;
    for (const h of holes.values()) {
        sumE += h.origin.x;
        sumN += h.origin.y;
    }
    const n = Math.max(1, holes.size);
    const cE = sumE / n;
    const cN = sumN / n;
    const shift = (p: XYZ, h: PlacedHole): XYZ => ({ x: h.origin.x - cE + p.x, y: h.origin.y - cN + p.y, z: h.origin.z + p.z });
    const shiftArrays = (a: { x: number[]; y: number[]; z: number[] }, h: PlacedHole): XYZArrays => ({
        x: a.x.map((v) => v + h.origin.x - cE),
        y: a.y.map((v) => v + h.origin.y - cN),
        z: a.z.map((v) => v + h.origin.z),
    });

    return {
        holes,
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
            return shiftArrays({
                x: h.path.map((p) => p.x),
                y: h.path.map((p) => p.y),
                z: h.path.map((p) => p.z),
            }, h);
        },
        caption: describeDesurvey(holes.values()),
        elevationKnownForAll: Array.from(holes.values()).every((h) => h.elevationKnown),
    };
}

/** z-axis title for a Scene3D. */
export function sceneZAxisTitle(scene: Pick<Scene3D, 'elevationKnownForAll'>): string {
    return scene.elevationKnownForAll ? 'Elevation (m)' : 'Elevation (m · collars without one at 0)';
}

/**
 * One-line caption for a set of placed holes, so a view can say how many are
 * drawn from real surveys and how many are projections.
 */
export function describeDesurvey(holes: Iterable<PlacedHole>): string {
    let surveyed = 0;
    let collarOnly = 0;
    let vertical = 0;
    for (const h of holes) {
        if (h.orientation === 'surveys') surveyed++;
        else if (h.orientation === 'collar') collarOnly++;
        else vertical++;
    }
    const parts = [`${surveyed} desurveyed (minimum curvature)`];
    if (collarOnly > 0) parts.push(`${collarOnly} unsurveyed — projected along collar azimuth/dip`);
    if (vertical > 0) parts.push(`${vertical} with no orientation — drawn vertical`);
    return parts.join(' · ');
}
