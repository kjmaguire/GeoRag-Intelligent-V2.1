/**
 * MapView layer activation tests — Module 8 Chunks 8.5 + 8.6.
 *
 * Verifies the MVT_LAYERS registry and MVT_INTERACTIVE_LAYERS array contain
 * the correct entries for seismic and geochem, and that their configuration
 * matches the spec from the architecture doc.
 *
 * No DOM rendering — all tests are pure registry inspection.
 */
import { describe, it, expect } from 'vitest';
import { MVT_LAYERS, MVT_INTERACTIVE_LAYERS, MVT_DEFAULT_VISIBILITY } from '../../lib/mvtLayers';

// ─── Registry completeness ─────────────────────────────────────────────────

describe('MVT_LAYERS registry', () => {
    it('contains a seismic entry', () => {
        const layer = MVT_LAYERS.find((l) => l.id === 'seismic');
        expect(layer).toBeDefined();
    });

    it('contains a geochem entry', () => {
        const layer = MVT_LAYERS.find((l) => l.id === 'geochem');
        expect(layer).toBeDefined();
    });

    it('contains all 7 silver layers', () => {
        const ids = MVT_LAYERS.map((l) => l.id);
        const required = [
            'collars',
            'traces',
            'boundaries',
            'formations',
            'historic-workings',
            'seismic',
            'geochem',
        ];
        for (const id of required) {
            expect(ids).toContain(id);
        }
    });
});

// ─── Seismic layer spec ────────────────────────────────────────────────────

describe('seismic layer', () => {
    const seismic = MVT_LAYERS.find((l) => l.id === 'seismic')!;

    it('has type "fill" (bbox polygon geometry)', () => {
        expect(seismic.type).toBe('fill');
    });

    it('uses pg_seismic_by_project as functionName', () => {
        expect(seismic.functionName).toBe('pg_seismic_by_project');
    });

    it('uses "seismic" as sourceLayer — confirmed from ST_AsMVT literal in migration', () => {
        expect(seismic.sourceLayer).toBe('seismic');
    });

    it('has fill-color set to sky-500 (#0ea5e9)', () => {
        expect(seismic.paint['fill-color']).toBe('#0ea5e9');
    });

    it('has fill-opacity set to 0.18', () => {
        expect(seismic.paint['fill-opacity']).toBe(0.18);
    });

    it('has an outline layer definition', () => {
        expect(seismic.outline).toBeDefined();
        expect(seismic.outline?.paint['line-color']).toBe('#0ea5e9');
    });

    it('has minzoom 4 (visible at regional scale)', () => {
        expect(seismic.minzoom).toBe(4);
    });
});

// ─── Geochem layer spec ────────────────────────────────────────────────────

describe('geochem layer', () => {
    const geochem = MVT_LAYERS.find((l) => l.id === 'geochem')!;

    it('has type "circle" (point geometry)', () => {
        expect(geochem.type).toBe('circle');
    });

    it('uses pg_geochem_by_project as functionName', () => {
        expect(geochem.functionName).toBe('pg_geochem_by_project');
    });

    it('uses "geochem" as sourceLayer — confirmed from ST_AsMVT literal in migration', () => {
        expect(geochem.sourceLayer).toBe('geochem');
    });

    it('has circle-color set to lime-500 (#84cc16)', () => {
        expect(geochem.paint['circle-color']).toBe('#84cc16');
    });

    it('has zoom-tiered circle-radius interpolation', () => {
        const radius = geochem.paint['circle-radius'];
        // Must be an interpolate expression
        expect(Array.isArray(radius)).toBe(true);
        expect(radius[0]).toBe('interpolate');
        // Must contain zoom stops (at least 3 stops)
        expect(radius.length).toBeGreaterThanOrEqual(6);
    });

    it('has minzoom 8 to hide at low zoom (too dense)', () => {
        // Geochem samples are dense at regional scale; hide below z8
        expect(geochem.minzoom).toBe(8);
    });

    it('has circle-opacity set to 0.85', () => {
        expect(geochem.paint['circle-opacity']).toBe(0.85);
    });
});

// ─── Interactive layers ────────────────────────────────────────────────────

describe('MVT_INTERACTIVE_LAYERS', () => {
    it('contains mvt-seismic for click/hover interactivity', () => {
        expect(MVT_INTERACTIVE_LAYERS).toContain('mvt-seismic');
    });

    it('contains mvt-geochem for click/hover interactivity', () => {
        expect(MVT_INTERACTIVE_LAYERS).toContain('mvt-geochem');
    });

    it('still contains mvt-collars (existing interactive layer)', () => {
        expect(MVT_INTERACTIVE_LAYERS).toContain('mvt-collars');
    });

    it('still contains mvt-historic-workings (existing interactive layer)', () => {
        expect(MVT_INTERACTIVE_LAYERS).toContain('mvt-historic-workings');
    });
});

// ─── Default visibility ────────────────────────────────────────────────────

describe('MVT_DEFAULT_VISIBILITY', () => {
    it('seismic defaults to visible (true)', () => {
        expect(MVT_DEFAULT_VISIBILITY['seismic']).toBe(true);
    });

    it('geochem defaults to visible (true)', () => {
        expect(MVT_DEFAULT_VISIBILITY['geochem']).toBe(true);
    });

    it('all 7 layers have a default visibility entry', () => {
        const required = [
            'collars', 'traces', 'boundaries', 'formations',
            'historic-workings', 'seismic', 'geochem',
        ];
        for (const id of required) {
            expect(MVT_DEFAULT_VISIBILITY).toHaveProperty(id);
            expect(typeof MVT_DEFAULT_VISIBILITY[id]).toBe('boolean');
        }
    });
});

// ─── sourceLayer strings confirmed against migration ──────────────────────

describe('sourceLayer values — confirmed from ST_AsMVT literals in migrations', () => {
    const getSourceLayer = (id: string): string => {
        const layer = MVT_LAYERS.find((l) => l.id === id);
        if (!layer) throw new Error(`Layer '${id}' not in MVT_LAYERS`);
        return layer.sourceLayer;
    };

    // Migration: 2026_04_22_130000_create_silver_mvt_functions.php
    it('collars sourceLayer = "collars" (ST_AsMVT(tile, \'collars\', 4096, \'geom\'))', () => {
        expect(getSourceLayer('collars')).toBe('collars');
    });

    it('traces sourceLayer = "drill_traces" (ST_AsMVT(tile, \'drill_traces\', 4096, \'geom\'))', () => {
        expect(getSourceLayer('traces')).toBe('drill_traces');
    });

    it('seismic sourceLayer = "seismic" (ST_AsMVT(tile, \'seismic\', 4096, \'geom\'))', () => {
        expect(getSourceLayer('seismic')).toBe('seismic');
    });

    // Migration: 2026_04_22_140000_create_silver_boundary_formation_working_geochem.php
    it('boundaries sourceLayer = "boundaries" (ST_AsMVT(tile, \'boundaries\', 4096, \'geom\'))', () => {
        expect(getSourceLayer('boundaries')).toBe('boundaries');
    });

    it('formations sourceLayer = "formations" (ST_AsMVT(tile, \'formations\', 4096, \'geom\'))', () => {
        expect(getSourceLayer('formations')).toBe('formations');
    });

    it('historic-workings sourceLayer = "historic_workings"', () => {
        expect(getSourceLayer('historic-workings')).toBe('historic_workings');
    });

    it('geochem sourceLayer = "geochem" (ST_AsMVT(tile, \'geochem\', 4096, \'geom\'))', () => {
        expect(getSourceLayer('geochem')).toBe('geochem');
    });
});
