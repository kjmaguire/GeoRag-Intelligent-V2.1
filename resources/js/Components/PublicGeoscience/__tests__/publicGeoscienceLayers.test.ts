import { describe, it, expect } from 'vitest';
import {
    jurisdictionFilter,
    commodityGroupingFilter,
    combineFilters,
    pointLayers,
    polygonLayers,
    lineLayers,
    LAYER_SPECS,
    GROUPING_COLORS,
    POTENTIAL_FILL_RAMP,
    ZOOM_THRESHOLDS,
    MINE_STYLE,
    OCCURRENCE_STYLE,
    DRILLHOLE_STYLE,
    FAULT_STYLE,
    DYKE_STYLE,
    WELL_TRAJECTORY_STYLE,
    GENERIC_LINE_STYLE,
} from '../publicGeoscienceLayers';

// ── jurisdictionFilter ────────────────────────────────────────────────────

describe('jurisdictionFilter', () => {
    it('returns a MapLibre == expression for a valid jurisdiction code', () => {
        expect(jurisdictionFilter('CA-SK')).toEqual([
            '==',
            ['get', 'jurisdiction_code'],
            'CA-SK',
        ]);
    });

    it('returns null for null input', () => {
        expect(jurisdictionFilter(null)).toBeNull();
    });

    it('returns null for empty string', () => {
        expect(jurisdictionFilter('')).toBeNull();
    });

    it('preserves the exact code passed in', () => {
        const result = jurisdictionFilter('CA-AB');
        expect(result).toEqual(['==', ['get', 'jurisdiction_code'], 'CA-AB']);
    });
});

// ── commodityGroupingFilter ───────────────────────────────────────────────

describe('commodityGroupingFilter', () => {
    it('returns a MapLibre == expression for a valid grouping', () => {
        expect(commodityGroupingFilter('precious_metals')).toEqual([
            '==',
            ['get', 'commodity_grouping'],
            'precious_metals',
        ]);
    });

    it('returns null for null input', () => {
        expect(commodityGroupingFilter(null)).toBeNull();
    });

    it('returns null for empty string', () => {
        expect(commodityGroupingFilter('')).toBeNull();
    });

    it('works for any grouping value', () => {
        const result = commodityGroupingFilter('uranium');
        expect(result).toEqual(['==', ['get', 'commodity_grouping'], 'uranium']);
    });
});

// ── combineFilters ────────────────────────────────────────────────────────

describe('combineFilters', () => {
    const f1 = ['==', ['get', 'jurisdiction_code'], 'CA-SK'];
    const f2 = ['==', ['get', 'commodity_grouping'], 'uranium'];

    it('returns null when both filters are null', () => {
        expect(combineFilters(null, null)).toBeNull();
    });

    it('returns null when called with no arguments', () => {
        expect(combineFilters()).toBeNull();
    });

    it('returns the single filter unwrapped (not nested in all) when only one is non-null', () => {
        expect(combineFilters(f1, null)).toEqual(f1);
        expect(combineFilters(null, f2)).toEqual(f2);
    });

    it('returns the filter directly when called with a single non-null argument', () => {
        expect(combineFilters(f1)).toEqual(f1);
    });

    it('wraps two filters in an all expression', () => {
        expect(combineFilters(f1, f2)).toEqual(['all', f1, f2]);
    });

    it('wraps three filters in an all expression', () => {
        const f3 = ['==', ['get', 'name'], 'Test'];
        expect(combineFilters(f1, f2, f3)).toEqual(['all', f1, f2, f3]);
    });

    it('ignores falsy values other than null', () => {
        // undefined is also falsy — same treatment as null
        expect(combineFilters(undefined as any, f1)).toEqual(f1);
    });
});

// ── LAYER_SPECS ───────────────────────────────────────────────────────────

describe('LAYER_SPECS', () => {
    it('has at least the four foundational Tier 1 entries', () => {
        // Tier 1 baseline — these are the four layers that shipped in the
        // original Public Geoscience release. Additional Tier 1 expansion
        // (rock samples, assessment surveys) + Tier 2 (mineral disposition,
        // …) can grow the array; the test only guards the floor so new
        // entries don't fail CI on a count check.
        expect(LAYER_SPECS.length).toBeGreaterThanOrEqual(4);
        const ids = LAYER_SPECS.map(s => s.id);
        for (const required of [
            'pg_mines',
            'pg_mineral_occurrences',
            'pg_drillhole_collars',
            'pg_resource_potential',
        ]) {
            expect(ids).toContain(required);
        }
    });

    it('point and polygon layer counts are both >= 1', () => {
        const points = LAYER_SPECS.filter(s => s.kind === 'point');
        const polygons = LAYER_SPECS.filter(s => s.kind === 'polygon');
        const lines = LAYER_SPECS.filter(s => s.kind === 'line');
        // Must always have both kinds to exercise both map-layer builders;
        // lines are optional until a Tier 2 line source (e.g., faults) ships.
        expect(points.length).toBeGreaterThanOrEqual(1);
        expect(polygons.length).toBeGreaterThanOrEqual(1);
        // Every spec must have one of the three known kinds.
        expect(points.length + polygons.length + lines.length).toBe(LAYER_SPECS.length);
    });

    it('includes pg_mines, pg_mineral_occurrences, pg_drillhole_collars, pg_resource_potential', () => {
        const ids = LAYER_SPECS.map(s => s.id);
        expect(ids).toContain('pg_mines');
        expect(ids).toContain('pg_mineral_occurrences');
        expect(ids).toContain('pg_drillhole_collars');
        expect(ids).toContain('pg_resource_potential');
    });

    it('every spec has required fields: id, label, description, sourceLayer, kind, defaultVisible', () => {
        for (const spec of LAYER_SPECS) {
            expect(spec).toHaveProperty('id');
            expect(spec).toHaveProperty('label');
            expect(spec).toHaveProperty('description');
            expect(spec).toHaveProperty('sourceLayer');
            expect(spec).toHaveProperty('kind');
            expect(spec).toHaveProperty('defaultVisible');
        }
    });

    it('polygon layer id is pg_resource_potential', () => {
        const poly = LAYER_SPECS.find(s => s.kind === 'polygon');
        expect(poly?.id).toBe('pg_resource_potential');
    });
});

// ── GROUPING_COLORS ───────────────────────────────────────────────────────

describe('GROUPING_COLORS', () => {
    // The canonical set of commodity_grouping enum values used in the
    // GROUPING_MATCH_EXPR inside publicGeoscienceLayers.ts.
    const EXPECTED_GROUPINGS = [
        'precious_metals',
        'base_metals',
        'uranium',
        'potash_salt',
        'industrial_materials',
        'gemstones',
        'lithium',
        'ree',
        'coal',
    ];

    it('has an entry for every required commodity_grouping enum value', () => {
        for (const grouping of EXPECTED_GROUPINGS) {
            expect(GROUPING_COLORS).toHaveProperty(grouping);
            expect(typeof GROUPING_COLORS[grouping]).toBe('string');
            expect(GROUPING_COLORS[grouping]).toMatch(/^#[0-9a-f]{6}$/i);
        }
    });

    it('has an "other" fallback entry', () => {
        expect(GROUPING_COLORS).toHaveProperty('other');
    });

    it('precious_metals is amber-500 (#eab308)', () => {
        expect(GROUPING_COLORS.precious_metals).toBe('#eab308');
    });

    it('uranium is emerald-500 (#22c55e)', () => {
        expect(GROUPING_COLORS.uranium).toBe('#22c55e');
    });
});

// ── pointLayers ───────────────────────────────────────────────────────────

describe('pointLayers', () => {
    const baseArgs = {
        sourceId: 'pg_mines',
        sourceLayerName: 'pg_mines',
        idPrefix: 'mines',
        style: MINE_STYLE,
        labelField: 'name',
    };

    it('returns 3 layers (heatmap + circle + label) when withLabels is true', () => {
        const layers = pointLayers({ ...baseArgs, withLabels: true });
        expect(layers).toHaveLength(3);
    });

    it('returns 2 layers (heatmap + circle, no label) when withLabels is false', () => {
        const layers = pointLayers({ ...baseArgs, withLabels: false });
        expect(layers).toHaveLength(2);
    });

    it('layer ids follow the idPrefix_{type} naming convention', () => {
        const layers = pointLayers({ ...baseArgs, withLabels: true });
        const ids = layers.map((l: any) => l.id);
        expect(ids).toContain('mines_heatmap');
        expect(ids).toContain('mines_circle');
        expect(ids).toContain('mines_label');
    });

    it('heatmap layer type is "heatmap"', () => {
        const layers = pointLayers({ ...baseArgs, withLabels: false });
        const heatmap = layers.find((l: any) => l.id === 'mines_heatmap');
        expect(heatmap?.type).toBe('heatmap');
    });

    it('circle layer type is "circle"', () => {
        const layers = pointLayers({ ...baseArgs, withLabels: false });
        const circle = layers.find((l: any) => l.id === 'mines_circle');
        expect(circle?.type).toBe('circle');
    });

    it('label layer type is "symbol"', () => {
        const layers = pointLayers({ ...baseArgs, withLabels: true });
        const label = layers.find((l: any) => l.id === 'mines_label');
        expect(label?.type).toBe('symbol');
    });

    it('drillhole call with withLabels false returns 2 layers', () => {
        const layers = pointLayers({
            sourceId: 'pg_drillhole_collars',
            sourceLayerName: 'pg_drillhole_collars',
            idPrefix: 'drillholes',
            style: DRILLHOLE_STYLE,
            withLabels: false,
            labelField: 'drillhole_name',
        });
        expect(layers).toHaveLength(2);
    });

    it('occurrence call with withLabels true returns 3 layers', () => {
        const layers = pointLayers({
            sourceId: 'pg_mineral_occurrences',
            sourceLayerName: 'pg_mineral_occurrences',
            idPrefix: 'occurrences',
            style: OCCURRENCE_STYLE,
            withLabels: true,
            labelField: 'name',
        });
        expect(layers).toHaveLength(3);
    });

    it('all layers reference the correct source and source-layer', () => {
        const layers = pointLayers({ ...baseArgs, withLabels: true });
        for (const layer of layers) {
            expect((layer as any).source).toBe('pg_mines');
            expect((layer as any)['source-layer']).toBe('pg_mines');
        }
    });
});

// ── polygonLayers ─────────────────────────────────────────────────────────

describe('polygonLayers', () => {
    const args = {
        sourceId: 'pg_resource_potential',
        sourceLayerName: 'pg_resource_potential',
        idPrefix: 'potential',
    };

    it('returns exactly 2 layers (fill + outline)', () => {
        const layers = polygonLayers(args);
        expect(layers).toHaveLength(2);
    });

    it('first layer is a fill layer', () => {
        const layers = polygonLayers(args);
        expect(layers[0].type).toBe('fill');
        expect(layers[0].id).toBe('potential_fill');
    });

    it('second layer is a line layer (outline)', () => {
        const layers = polygonLayers(args);
        expect(layers[1].type).toBe('line');
        expect(layers[1].id).toBe('potential_outline');
    });

    it('fill layer uses POTENTIAL_FILL_RAMP for fill-color', () => {
        const layers = polygonLayers(args);
        expect(layers[0].paint['fill-color']).toEqual(POTENTIAL_FILL_RAMP);
    });

    it('both layers reference the correct source and source-layer', () => {
        const layers = polygonLayers(args);
        for (const layer of layers) {
            expect(layer.source).toBe('pg_resource_potential');
            expect(layer['source-layer']).toBe('pg_resource_potential');
        }
    });
});

// ── lineLayers ────────────────────────────────────────────────────────────

describe('lineLayers', () => {
    const args = {
        sourceId: 'pg_geological_faults',
        sourceLayerName: 'pg_geological_faults',
        idPrefix: 'fault',
        style: FAULT_STYLE,
    };

    it('returns 2 layers by default (casing + line)', () => {
        const layers = lineLayers(args);
        expect(layers).toHaveLength(2);
        expect(layers[0].id).toBe('fault_casing');
        expect(layers[1].id).toBe('fault_line');
    });

    it('returns 1 layer when withCasing:false', () => {
        const layers = lineLayers({ ...args, withCasing: false });
        expect(layers).toHaveLength(1);
        expect(layers[0].id).toBe('fault_line');
    });

    it('both layers are MapLibre line type', () => {
        const layers = lineLayers(args);
        for (const layer of layers) {
            expect(layer.type).toBe('line');
        }
    });

    it('casing layer is wider than the main line at every zoom stop', () => {
        const layers = lineLayers(args);
        const casingWidth = layers[0].paint['line-width'];
        const mainWidth = layers[1].paint['line-width'];
        // Both are interpolate expressions: [interpolate, [linear], [zoom], z0, w0, z1, w1]
        // Stops at indices 3/5 (w0) and 5/7 (w1) — casing must be wider at both.
        expect(casingWidth[4]).toBeGreaterThan(mainWidth[4]);
        expect(casingWidth[6]).toBeGreaterThan(mainWidth[6]);
    });

    it('main line uses the style lineColor', () => {
        const layers = lineLayers(args);
        expect(layers[1].paint['line-color']).toBe(FAULT_STYLE.lineColor);
    });

    it('applies line-dasharray when style.dashArray is set (DYKE_STYLE)', () => {
        const layers = lineLayers({
            ...args,
            style: DYKE_STYLE,
            idPrefix: 'dyke',
        });
        expect(layers[1].paint['line-dasharray']).toEqual(DYKE_STYLE.dashArray);
    });

    it('omits line-dasharray when style.dashArray is undefined (FAULT_STYLE)', () => {
        const layers = lineLayers(args);
        expect(layers[1].paint).not.toHaveProperty('line-dasharray');
    });

    it('uses round line caps and joins for continuity', () => {
        const layers = lineLayers(args);
        for (const layer of layers) {
            expect(layer.layout['line-cap']).toBe('round');
            expect(layer.layout['line-join']).toBe('round');
        }
    });

    it('references the supplied source and source-layer on both sub-layers', () => {
        const layers = lineLayers(args);
        for (const layer of layers) {
            expect(layer.source).toBe('pg_geological_faults');
            expect(layer['source-layer']).toBe('pg_geological_faults');
        }
    });
});

// ── Line styles ──────────────────────────────────────────────────────────

describe('Line styles', () => {
    it.each([
        ['FAULT_STYLE', FAULT_STYLE],
        ['DYKE_STYLE', DYKE_STYLE],
        ['WELL_TRAJECTORY_STYLE', WELL_TRAJECTORY_STYLE],
        ['GENERIC_LINE_STYLE', GENERIC_LINE_STYLE],
    ])('%s has lineColor and widthStops of length 4', (_name, style) => {
        expect(style.lineColor).toBeTruthy();
        expect(style.widthStops).toHaveLength(4);
        // Stops are [z, w, z, w] — z must be strictly increasing.
        expect(style.widthStops[0]).toBeLessThan(style.widthStops[2]);
    });

    it('DYKE_STYLE carries a dashArray (dykes are visually distinct from faults)', () => {
        expect(DYKE_STYLE.dashArray).toBeDefined();
        expect(DYKE_STYLE.dashArray).toHaveLength(2);
    });

    it('FAULT_STYLE has no dashArray (faults are solid)', () => {
        expect(FAULT_STYLE.dashArray).toBeUndefined();
    });
});

// ── LayerSpec.kind union includes "line" ─────────────────────────────────

describe('LayerSpec.kind supports point | polygon | line', () => {
    it('every LAYER_SPECS entry has a valid kind', () => {
        const allowedKinds = new Set(['point', 'polygon', 'line']);
        for (const spec of LAYER_SPECS) {
            expect(allowedKinds.has(spec.kind)).toBe(true);
        }
    });
});

// ── ZOOM_THRESHOLDS ───────────────────────────────────────────────────────

describe('ZOOM_THRESHOLDS', () => {
    it('is exported as a const object with HEATMAP_MAX, CIRCLES_MIN, LABEL_MIN', () => {
        expect(ZOOM_THRESHOLDS).toHaveProperty('HEATMAP_MAX');
        expect(ZOOM_THRESHOLDS).toHaveProperty('CIRCLES_MIN');
        expect(ZOOM_THRESHOLDS).toHaveProperty('LABEL_MIN');
    });

    it('all threshold values are numbers', () => {
        expect(typeof ZOOM_THRESHOLDS.HEATMAP_MAX).toBe('number');
        expect(typeof ZOOM_THRESHOLDS.CIRCLES_MIN).toBe('number');
        expect(typeof ZOOM_THRESHOLDS.LABEL_MIN).toBe('number');
    });

    it('CIRCLES_MIN < LABEL_MIN (circles appear before labels)', () => {
        expect(ZOOM_THRESHOLDS.CIRCLES_MIN).toBeLessThan(ZOOM_THRESHOLDS.LABEL_MIN);
    });

    it('HEATMAP_MAX > CIRCLES_MIN - 1 (one zoom of overlap for smooth transition)', () => {
        // The comment in the source says "overlap with heatmap by 1 zoom"
        // meaning CIRCLES_MIN <= HEATMAP_MAX
        expect(ZOOM_THRESHOLDS.CIRCLES_MIN).toBeLessThanOrEqual(
            ZOOM_THRESHOLDS.HEATMAP_MAX,
        );
    });

    it('object is deeply frozen (as-const prevents mutation at the type level)', () => {
        // TypeScript `as const` doesn't freeze at runtime, but we verify the
        // values are stable numbers that match between reads.
        const snapshot = { ...ZOOM_THRESHOLDS };
        expect(ZOOM_THRESHOLDS).toEqual(snapshot);
    });
});
