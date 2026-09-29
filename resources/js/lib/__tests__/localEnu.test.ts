import { describe, expect, it } from 'vitest';
import { centroidOrigin, metresPerDegree, toLocalMetres } from '@/lib/localEnu';

describe('localEnu (GIS-9)', () => {
    it('has the right ground distance per degree', () => {
        // WGS84 reference values: at the equator 111,319.5 m/° lon,
        // 110,574.3 m/° lat; at 58°N 59,132.9 m/° lon, 111,377.6 m/° lat.
        expect(metresPerDegree(0).lon).toBeCloseTo(111319.5, 0);
        expect(metresPerDegree(0).lat).toBeCloseTo(110574.3, 0);
        expect(metresPerDegree(58).lon).toBeCloseTo(59132.9, 0);
        expect(metresPerDegree(58).lat).toBeCloseTo(111377.6, 0);
    });

    it('at 58°N a degree of longitude is about half a degree of latitude on the ground', () => {
        const k = metresPerDegree(58);
        expect(k.lon / k.lat).toBeCloseTo(0.53, 2);
    });

    it('converts about the centroid', () => {
        const o = centroidOrigin([{ lon: -105, lat: 58 }, { lon: -104.99, lat: 58.01 }])!;
        const f = toLocalMetres(o);
        const a = f(-105, 58);
        const b = f(-104.99, 58.01);
        expect(a.east).toBeCloseTo(-b.east, 6);
        expect(b.north - a.north).toBeCloseTo(1113.8, 0);
    });

    it('returns null origin for no finite points', () => {
        expect(centroidOrigin([{ lon: NaN, lat: 1 }])).toBeNull();
    });
});
