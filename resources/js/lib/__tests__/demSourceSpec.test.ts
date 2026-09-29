/**
 * FE-19 — WorkspaceMap's terrain comes from the basemap registry, through the
 * same source builder MapView uses.
 */
import { describe, expect, it } from 'vitest';
import { demSourceSpec } from '@/lib/basemap';
import workspaceMapSource from '../../Components/Foundry/WorkspaceMap.tsx?raw';

describe('demSourceSpec', () => {
    it('passes a TileJSON URL through, letting the TileJSON declare its encoding', () => {
        const spec = demSourceSpec('https://tiles.example.test/dem/tilejson.json');
        expect(spec).toMatchObject({ type: 'raster-dem', url: 'https://tiles.example.test/dem/tilejson.json' });
        expect(spec).not.toHaveProperty('encoding');
    });

    it('wraps a raw template and infers terrarium from the path', () => {
        expect(demSourceSpec('https://dem.local/terrarium/{z}/{x}/{y}.png')).toMatchObject({
            type: 'raster-dem',
            tiles: ['https://dem.local/terrarium/{z}/{x}/{y}.png'],
            encoding: 'terrarium',
        });
    });

    it('defaults a raw template to Mapbox terrain-RGB', () => {
        expect(demSourceSpec('https://dem.local/rgb/{z}/{x}/{y}.png')).toMatchObject({ encoding: 'mapbox' });
    });
});

describe('WorkspaceMap source', () => {
    const src = workspaceMapSource;

    it('no longer hard-codes the AWS terrarium bucket', () => {
        expect(src).not.toContain('elevation-tiles-prod');
        expect(src).toContain('useTerrainDemUrl()');
    });

    it('draws no synthetic trace layer (FE-7 / GIS-10)', () => {
        expect(src).not.toContain("'collar-traces'");
        expect(src).not.toContain('collar-traces-line');
    });
});
