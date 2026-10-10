/**
 * Longitude/latitude → local east/north metres about an origin.
 *
 * GIS-9 (2026-09-29): DrillTrace3D plotted x = longitude, y = latitude (in
 * degrees) against z = elevation (in metres) with Plotly's automatic aspect,
 * so apparent dips and azimuths depended on the data extent — and at 58°N a
 * degree of longitude is about half a degree of latitude on the ground. A
 * geologist reading hole attitude from that card was misled.
 *
 * This is a local tangent-plane approximation on the WGS84 ellipsoid using
 * the meridional (M) and prime-vertical (N) radii of curvature at the origin
 * latitude. Over a drill project (a few km) its error is centimetres, which
 * is far below anything the 3D card can show; it is NOT a substitute for a
 * proper projected CRS over regional extents.
 */

const A = 6378137.0;
const E2 = 0.00669437999014;
const DEG = Math.PI / 180;

export interface LonLatOrigin {
    lon: number;
    lat: number;
}

/** Metres per degree of longitude and latitude at `latDeg`. */
export function metresPerDegree(latDeg: number): { lon: number; lat: number } {
    const phi = latDeg * DEG;
    const s2 = Math.sin(phi) ** 2;
    const n = A / Math.sqrt(1 - E2 * s2);
    const m = (A * (1 - E2)) / (1 - E2 * s2) ** 1.5;
    return { lon: n * Math.cos(phi) * DEG, lat: m * DEG };
}

/**
 * A longitude difference as the short way round, in [-180, 180].
 *
 * A project that straddles the antimeridian has collars at 179.9° and
 * -179.9°: subtracting the raw values is 359.8° (about 20,000 km) rather than
 * the 0.2° (about 13 km) between them.
 */
export function wrapLonDelta(deltaDeg: number): number {
    return ((((deltaDeg + 180) % 360) + 360) % 360) - 180;
}

/**
 * Mean of the given points, as the ENU origin. Longitudes are averaged about
 * the first one (by the short way round), so a project that straddles the
 * antimeridian gets an origin inside it, not on the far side of the earth.
 */
export function centroidOrigin(points: ReadonlyArray<{ lon: number; lat: number }>): LonLatOrigin | null {
    const valid = points.filter((p) => Number.isFinite(p.lon) && Number.isFinite(p.lat));
    if (valid.length === 0) return null;
    const ref = valid[0].lon;
    const meanDelta = valid.reduce((s, p) => s + wrapLonDelta(p.lon - ref), 0) / valid.length;
    return {
        lon: wrapLonDelta(ref + meanDelta),
        lat: valid.reduce((s, p) => s + p.lat, 0) / valid.length,
    };
}

/** Build a converter from lon/lat to east/north metres about `origin`. */
export function toLocalMetres(origin: LonLatOrigin): (lon: number, lat: number) => { east: number; north: number } {
    const k = metresPerDegree(origin.lat);
    return (lon, lat) => ({ east: wrapLonDelta(lon - origin.lon) * k.lon, north: (lat - origin.lat) * k.lat });
}
