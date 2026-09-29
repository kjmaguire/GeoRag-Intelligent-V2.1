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

/** Mean of the given points, as the ENU origin. */
export function centroidOrigin(points: ReadonlyArray<{ lon: number; lat: number }>): LonLatOrigin | null {
    const valid = points.filter((p) => Number.isFinite(p.lon) && Number.isFinite(p.lat));
    if (valid.length === 0) return null;
    return {
        lon: valid.reduce((s, p) => s + p.lon, 0) / valid.length,
        lat: valid.reduce((s, p) => s + p.lat, 0) / valid.length,
    };
}

/** Build a converter from lon/lat to east/north metres about `origin`. */
export function toLocalMetres(origin: LonLatOrigin): (lon: number, lat: number) => { east: number; north: number } {
    const k = metresPerDegree(origin.lat);
    return (lon, lat) => ({ east: (lon - origin.lon) * k.lon, north: (lat - origin.lat) * k.lat });
}
