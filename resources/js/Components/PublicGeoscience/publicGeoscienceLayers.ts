/**
 * Layer style definitions for Public Geoscience MVT sources.
 *
 * Four Martin sources (see docker/martin/martin.yaml and plan §09a):
 *   pg_mines               → point layer (zoom-tiered: heatmap + circle + symbol)
 *   pg_mineral_occurrences → point layer (same tiering)
 *   pg_drillhole_collars   → point layer (tiered; appears later than occurrences)
 *   pg_resource_potential  → polygon layer (choropleth by potential_rank)
 *
 * Intentional deviation from plan §09a:
 *   The plan prescribes `cluster: true` on MVT sources. MapLibre GL 5.x
 *   does not support clustering on `type: 'vector'` sources — it is a
 *   GeoJSON-only feature. We achieve the same product intent ("visual
 *   density signal at provincial scale, individual-point access at closer
 *   zooms") via a zoom-interpolated stack of heatmap + circle + symbol
 *   layers. No behavior delta from the user's perspective; measurable
 *   client performance win from keeping MVT.
 */

export type LayerId =
    // ── Tier 1 (Phases 1–4 — live) ──────────────────────────────────────
    | 'pg_mines'
    | 'pg_mineral_occurrences'
    | 'pg_drillhole_collars'
    | 'pg_resource_potential'
    | 'pg_rock_samples'
    | 'pg_assessment_surveys'
    // ── SMDI standalone (plan v1.1, 2026-05-24) ─────────────────────────
    // Backed by public.smdi_deposits — 6 K SK FeatureServer points.
    | 'smdi_deposits'
    // ── Tier 2+3 (Phases 2–6 — RESERVED) ───────────────────────────────
    // Declared in the union so per-source style dispatch in
    // PublicGeoscienceMap.tsx can reference them without TypeScript
    // narrowing to `never`. Activation is gated by LAYER_SPECS (which
    // does not yet include them) AND by the Martin yaml + TileProxy
    // whitelist (both commented out until the underlying canonical
    // tables + Silver assets land). See docs/field-inventory-sk-tier2-tier3.md
    // for schema contracts.
    | 'pg_mineral_dispositions'
    | 'pg_bedrock_geology'
    | 'pg_surficial_geology'
    | 'pg_geological_faults'
    | 'pg_geological_dykes'
    | 'pg_geological_feature_points'
    | 'pg_geological_feature_lines'
    | 'pg_petroleum_wells'
    | 'pg_petroleum_well_trajectories'
    | 'pg_petroleum_pools'
    | 'pg_geophysics_control_points'
    | 'pg_geophysics_survey_coverage'
    | 'pg_geological_domains'
    | 'pg_regional_compilation_points'
    | 'pg_regional_compilation_polygons'
    | 'pg_geoscience_publications'
    | 'pg_geochronology_samples'
    | 'pg_geochemistry_samples';

export interface LayerSpec {
    id: LayerId;
    label: string;
    description: string;
    /** MVT source layer name (matches the Martin `table_sources` key). */
    sourceLayer: LayerId;
    /** Geometry kind — drives which sub-layers we add to the map. */
    kind: 'point' | 'polygon' | 'line';
    /** Default visibility on first load. */
    defaultVisible: boolean;
}

export const LAYER_SPECS: LayerSpec[] = [
    {
        id: 'pg_mines',
        label: 'Mines',
        description: 'Operating and historic mine sites',
        sourceLayer: 'pg_mines',
        kind: 'point',
        defaultVisible: true,
    },
    {
        id: 'pg_mineral_occurrences',
        label: 'Mineral Occurrences',
        description: 'Showings, prospects, deposits (SMDI)',
        sourceLayer: 'pg_mineral_occurrences',
        kind: 'point',
        defaultVisible: true,
    },
    {
        id: 'pg_drillhole_collars',
        label: 'Drillhole Collars',
        description: 'Public drillhole compilation (collar-level)',
        sourceLayer: 'pg_drillhole_collars',
        kind: 'point',
        defaultVisible: true,
    },
    {
        id: 'pg_resource_potential',
        label: 'Resource Potential',
        description: 'Per-commodity potential polygons',
        sourceLayer: 'pg_resource_potential',
        kind: 'polygon',
        defaultVisible: true,
    },
    {
        id: 'pg_rock_samples',
        label: 'Rock Samples',
        description: 'Government-collected rock sample locations',
        sourceLayer: 'pg_rock_samples',
        kind: 'point',
        defaultVisible: false,
    },
    {
        id: 'pg_assessment_surveys',
        label: 'Assessment Surveys',
        description: 'SMAD survey footprints (airborne, ground, underground)',
        sourceLayer: 'pg_assessment_surveys',
        kind: 'polygon',
        defaultVisible: false,
    },
    {
        id: 'pg_mineral_dispositions',
        label: 'Mineral Dispositions',
        description: 'Active + legacy + pending tenure (SK Mining + Crown O&G)',
        sourceLayer: 'pg_mineral_dispositions',
        kind: 'polygon',
        // Off by default — turning it on before Silver has populated the
        // table would show an empty layer. Once the Dagster Bronze + Silver
        // for mineral_disposition have been materialized at least once the
        // default can flip to true.
        defaultVisible: false,
    },
    {
        id: 'pg_bedrock_geology',
        label: 'Bedrock Geology (250K)',
        description: 'SK bedrock units (eon/era/period/formation/member)',
        sourceLayer: 'pg_bedrock_geology',
        kind: 'polygon',
        // NOTE: polygonLayers() currently fills by POTENTIAL_FILL_RAMP which
        // reads potential_rank — bedrock has no such field, so polygons
        // render in the null-rank gray (#e5e7eb). Acceptable for the canary;
        // a follow-up should introduce a period/era-driven palette or let
        // LayerSpec carry a per-layer fill expression. See Phase B.
        defaultVisible: false,
    },
    {
        // ── SMDI standalone (plan v1.1, 2026-05-24) ──────────────────────
        // Backed by public.smdi_deposits — 6,012 SK mineral-deposit points
        // pulled live from the egis/Mineral_Exploration FeatureServer/2.
        // Parallel to pg_mineral_occurrences; default OFF because 6 K points
        // overlap heavily at provincial zoom. See
        // docs/handoffs/smdi_ingestion_2026_05_25.md for the unification
        // question.
        id: 'smdi_deposits',
        label: 'SMDI Mineral Deposits (SK)',
        description: 'Saskatchewan Mineral Deposit Index — 6,012 points (live feed)',
        sourceLayer: 'smdi_deposits',
        kind: 'point',
        defaultVisible: false,
    },
];

// ── Color palette ───────────────────────────────────────────────────────
// Driven by `commodity_grouping` so "at a glance" a geologist can tell a
// uranium occurrence from a gold one without reading the popup. The palette
// is deliberately high-contrast against the Positron basemap.
// WCAG AA audited against bg-gray-950 (#030712). Every color must achieve
// ≥ 4.5:1 contrast ratio for text and ≥ 3:1 for UI components. The two
// original failures (coal slate-700 at 1.94:1 and other gray-500 at 4.16:1)
// were replaced with lighter variants that pass while preserving visual
// distinctness for color-blind users (warm vs. cool gray tones).
export const GROUPING_COLORS: Record<string, string> = {
    precious_metals:      '#eab308', // amber-500    — 10.50:1 ✓
    base_metals:          '#0ea5e9', // sky-500      —  7.26:1 ✓
    uranium:              '#22c55e', // emerald-500  —  8.84:1 ✓
    potash_salt:          '#c084fc', // purple-400   —  7.62:1 ✓
    industrial_materials: '#94a3b8', // slate-400    —  7.85:1 ✓
    gemstones:            '#f472b6', // pink-400     —  7.60:1 ✓
    lithium:              '#f97316', // orange-500   —  7.18:1 ✓
    ree:                  '#f43f5e', // rose-500     —  5.48:1 ✓ (AA)
    coal:                 '#a8a29e', // stone-400    —  7.98:1 ✓ (was slate-700 FAIL)
    other:                '#9ca3af', // gray-400     —  7.93:1 ✓ (was gray-500 FAIL)
};

const GROUPING_MATCH_EXPR: any = [
    'match',
    ['get', 'commodity_grouping'],
    'precious_metals',      GROUPING_COLORS.precious_metals,
    'base_metals',          GROUPING_COLORS.base_metals,
    'uranium',              GROUPING_COLORS.uranium,
    'potash_salt',          GROUPING_COLORS.potash_salt,
    'industrial_materials', GROUPING_COLORS.industrial_materials,
    'gemstones',            GROUPING_COLORS.gemstones,
    'lithium',              GROUPING_COLORS.lithium,
    'ree',                  GROUPING_COLORS.ree,
    'coal',                 GROUPING_COLORS.coal,
    GROUPING_COLORS.other,
];

// Zoom thresholds carry product intent, not just cartography.
//   provincial <= 6  — "where in SK is the activity concentrated?" → heatmap
//   regional   6–10  — "what's around my target area?"             → circles
//   property   >= 10 — "show me the individual features"           → circles + labels
const HEATMAP_MAX = 7;
const CIRCLES_MIN = 6;   // overlap with heatmap by 1 zoom for smooth transition
const LABEL_MIN   = 11;

// ── Paint expressions — points ──────────────────────────────────────────

export interface PointLayerStyle {
    heatmapColor: string;   // single-commodity-category fallback for heatmap
    circleBaseRadius: [number, number, number, number]; // [z, r, z, r] stops
    /**
     * Optional monochrome dot color. When set, overrides the default
     * commodity_grouping match expression — use for layers that don't
     * carry commodity metadata (e.g. drillhole collars) and should read
     * as a single accent color. `colorExpr` arg to pointLayers still
     * wins over this if both are supplied.
     */
    circleColor?: string;
    /**
     * Emit a `{id}_halo` sub-layer under the dot (translucent fill +
     * stroke at ~2× the dot radius). Without `haloColor`, the halo
     * reuses the dot's color expression — so commodity-coloured layers
     * (mines, occurrences, SMDI, rock samples) get per-commodity halos
     * that match each dot. Workspace's ore-bearing collars read this
     * way; this flag extends that visual language across PG.
     */
    withHalo?: boolean;
    /**
     * Explicit halo color override (literal string). Use only when the
     * halo should differ from the dot color — e.g. drillholes pair a
     * `#8fe28b` dot with a slightly-darker `#7dd97c` halo so the dot
     * still reads as the foreground.
     */
    haloColor?: string;
}

export const MINE_STYLE: PointLayerStyle = {
    heatmapColor: GROUPING_COLORS.precious_metals,
    circleBaseRadius: [4, 2, 14, 9],
    withHalo: true,
};

export const OCCURRENCE_STYLE: PointLayerStyle = {
    heatmapColor: '#f59e0b',
    circleBaseRadius: [4, 1.6, 14, 7.5],
    withHalo: true,
};

// Drillhole collars — match Workspace's #8fe28b dot + #7dd97c halo so the
// public-geoscience surface reads as the same map family as the
// project-scoped Workspace canvas.
export const DRILLHOLE_STYLE: PointLayerStyle = {
    heatmapColor: '#7dd97c',
    circleColor: '#8fe28b',
    haloColor: '#7dd97c',
    // Bumped radii so collars are visible from regional zoom — the old
    // [6, 0.8, 14, 4] reads as pinpricks against the dark basemap.
    circleBaseRadius: [4, 2, 14, 9],
};

export const ROCK_SAMPLE_STYLE: PointLayerStyle = {
    heatmapColor: '#a8a29e',
    // Slightly smaller than mines/occurrences — rock samples are 10×
    // denser, so the halo + circle stack at full mine size would crowd
    // the map at zoom 10–12.
    circleBaseRadius: [4, 1.4, 14, 6],
    withHalo: true,
};

// ── SMDI style — separate from the snake_case commodity_grouping ────────
// The standalone public.smdi_deposits table preserves the upstream
// SYMBOLOGY_GROUPING values verbatim (e.g. "Base Metals", "Precious
// Metals", "Uranium"). Reuses the WCAG-audited GROUPING_COLORS palette so
// it's visually consistent with the canonical pg_mineral_occurrences
// layer when a user toggles between them.
export const SMDI_GROUPING_MATCH_EXPR: any = [
    'match',
    ['get', 'symbology_grouping'],
    'Precious Metals',      GROUPING_COLORS.precious_metals,
    'Base Metals',          GROUPING_COLORS.base_metals,
    'Uranium',              GROUPING_COLORS.uranium,
    'Potash / Salt',        GROUPING_COLORS.potash_salt,
    'Industrial Materials', GROUPING_COLORS.industrial_materials,
    'Gemstones',            GROUPING_COLORS.gemstones,
    'Lithium',              GROUPING_COLORS.lithium,
    'Rare Earth Elements',  GROUPING_COLORS.ree,
    'Coal',                 GROUPING_COLORS.coal,
    'Helium',               '#7dd3fc', // sky-300 — gas, distinct from sky-500 base_metals
    GROUPING_COLORS.other,
];

export const SMDI_STYLE: PointLayerStyle = {
    heatmapColor: GROUPING_COLORS.base_metals, // largest commodity group (1,786 / 6,012)
    circleBaseRadius: [4, 1.6, 14, 7.5],
    withHalo: true,
};

// Choropleth ramp for potential_rank 1..6 (6 = highest).
export const POTENTIAL_FILL_RAMP: any = [
    'interpolate',
    ['linear'],
    ['coalesce', ['to-number', ['get', 'potential_rank']], 0],
    0, '#e5e7eb', // gray-200 — no rank available
    1, '#fee2e2', // red-100
    2, '#fecaca', // red-200
    3, '#fca5a5', // red-300
    4, '#f87171', // red-400
    5, '#ef4444', // red-500
    6, '#b91c1c', // red-700
];

/**
 * Build the MapLibre style layer definitions for one point MVT source.
 *
 * Returns three layers in draw order:
 *   {id}_heatmap  — visible z <= HEATMAP_MAX
 *   {id}_circle   — visible z >= CIRCLES_MIN
 *   {id}_label    — visible z >= LABEL_MIN (only for mines + occurrences —
 *                   drillhole labels are too dense at any zoom)
 */
export function pointLayers(args: {
    sourceId: string;
    sourceLayerName: string;
    idPrefix: string;
    style: PointLayerStyle;
    withLabels: boolean;
    labelField: string; // e.g. 'name', 'drillhole_name'
    /**
     * Optional override for the circle-color expression. Defaults to the
     * shared snake_case `commodity_grouping` matcher; the SMDI layer
     * supplies a TitleCase `symbology_grouping` matcher because the
     * standalone public.smdi_deposits table preserves upstream field
     * values verbatim (plan v1.1).
     */
    colorExpr?: any;
}): any[] {
    const { sourceId, sourceLayerName, idPrefix, style, withLabels, labelField, colorExpr } = args;
    const fillExpr: any = colorExpr ?? style.circleColor ?? GROUPING_MATCH_EXPR;

    const layers: any[] = [
        // Density heatmap — visible at provincial scale.
        {
            id: `${idPrefix}_heatmap`,
            type: 'heatmap',
            source: sourceId,
            'source-layer': sourceLayerName,
            maxzoom: HEATMAP_MAX + 1,
            paint: {
                'heatmap-weight': 1,
                'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 0, 0.6, HEATMAP_MAX, 1.4],
                'heatmap-radius':    ['interpolate', ['linear'], ['zoom'], 0, 8,   HEATMAP_MAX, 24],
                'heatmap-opacity':   ['interpolate', ['linear'], ['zoom'], HEATMAP_MAX - 1, 0.7, HEATMAP_MAX + 1, 0],
                'heatmap-color': [
                    'interpolate', ['linear'], ['heatmap-density'],
                    0,   'rgba(0, 0, 0, 0)',
                    0.2, 'rgba(253, 224, 71, 0.4)',
                    0.5, 'rgba(251, 146, 60, 0.7)',
                    1.0, 'rgba(220, 38, 38, 0.9)',
                ],
            },
        },
    ];

    // Optional halo — drawn BEFORE the dot so the dot sits on top.
    // Matches Workspace's ore-bearing collar halo (translucent fill +
    // stroke). Same zoom range as the circle so the halo appears as soon
    // as individual points become visible.
    //
    // Color resolution: explicit haloColor wins; otherwise reuse the
    // fill expression so commodity-coloured layers get per-commodity
    // halos automatically (uranium dot → uranium halo, etc.).
    const haloPaintColor: any = style.haloColor ?? fillExpr;
    if (style.haloColor || style.withHalo) {
        layers.push({
            id: `${idPrefix}_halo`,
            type: 'circle',
            source: sourceId,
            'source-layer': sourceLayerName,
            minzoom: CIRCLES_MIN,
            paint: {
                'circle-radius': [
                    'interpolate', ['linear'], ['zoom'],
                    style.circleBaseRadius[0], style.circleBaseRadius[1] * 2.2,
                    style.circleBaseRadius[2], style.circleBaseRadius[3] * 2,
                ],
                'circle-color': haloPaintColor,
                'circle-opacity': 0.28,
                'circle-stroke-color': haloPaintColor,
                'circle-stroke-width': 1.5,
                'circle-stroke-opacity': 0.9,
            },
        });
    }

    // Individual circles — visible mid→high zoom. Drawn AFTER the halo
    // so the dot reads as the foreground marker.
    layers.push({
        id: `${idPrefix}_circle`,
        type: 'circle',
        source: sourceId,
        'source-layer': sourceLayerName,
        minzoom: CIRCLES_MIN,
        paint: {
            'circle-radius': [
                'interpolate', ['linear'], ['zoom'],
                style.circleBaseRadius[0], style.circleBaseRadius[1],
                style.circleBaseRadius[2], style.circleBaseRadius[3],
            ],
            'circle-color': fillExpr,
            'circle-stroke-color': '#0a0e14', // Foundry bg-0 — matches Workspace dot stroke
            'circle-stroke-width': [
                'interpolate', ['linear'], ['zoom'],
                CIRCLES_MIN, 0.4,
                14, 1.5,
            ],
            'circle-opacity': [
                'interpolate', ['linear'], ['zoom'],
                CIRCLES_MIN - 1, 0,
                CIRCLES_MIN + 1, 1,
            ],
        },
    });

    if (withLabels) {
        layers.push({
            id: `${idPrefix}_label`,
            type: 'symbol',
            source: sourceId,
            'source-layer': sourceLayerName,
            minzoom: LABEL_MIN,
            layout: {
                'text-field': ['coalesce', ['get', labelField], ''],
                'text-size': 11,
                'text-anchor': 'top',
                'text-offset': [0, 0.8],
                'text-allow-overlap': false,
                'text-optional': true,
            },
            paint: {
                'text-color': '#0f172a',
                'text-halo-color': '#ffffff',
                'text-halo-width': 1.2,
            },
        });
    }

    return layers;
}

/**
 * Polyline style for the line-geometry MVT sources (faults, dykes,
 * non-vertical well trajectories, EM conductors, etc.).
 *
 * One sub-layer only (vs. point's three-tier heatmap+circle+label and
 * polygon's fill+outline). Width scales with zoom and color comes from the
 * optional `lineColorExpression` argument; default is slate-600 neutral so
 * lines read as "structural overlay" context without competing with the
 * commodity-colored points.
 */
export interface LineLayerStyle {
    /** MapLibre paint color expression, or a static color string. */
    lineColor: string | any;
    /** Zoom→width interpolation stops: [z, w, z, w]. */
    widthStops: [number, number, number, number];
    /** Line dash pattern (optional — use for dykes or inferred/concealed faults). */
    dashArray?: [number, number];
}

export const FAULT_STYLE: LineLayerStyle = {
    lineColor: '#7f1d1d', // red-900 — reads as "tectonic break" against any basemap
    widthStops: [4, 0.4, 14, 2.2],
};

export const DYKE_STYLE: LineLayerStyle = {
    lineColor: '#581c87', // purple-900
    widthStops: [4, 0.4, 14, 2.0],
    dashArray: [2, 1],
};

export const WELL_TRAJECTORY_STYLE: LineLayerStyle = {
    lineColor: '#1e3a8a', // blue-900
    widthStops: [6, 0.4, 14, 1.6],
};

export const GENERIC_LINE_STYLE: LineLayerStyle = {
    lineColor: '#475569', // slate-600
    widthStops: [4, 0.3, 14, 1.5],
};

/**
 * Build the MapLibre style layer definitions for one line-geometry MVT
 * source. Emits a single `line` sub-layer with zoom-interpolated width and
 * opacity, plus an optional `_casing` under-layer for visual separation from
 * the basemap when lines are thin and dark.
 */
export function lineLayers(args: {
    sourceId: string;
    sourceLayerName: string;
    idPrefix: string;
    style: LineLayerStyle;
    withCasing?: boolean;
}): any[] {
    const { sourceId, sourceLayerName, idPrefix, style, withCasing = true } = args;

    const widthExpr: any = [
        'interpolate', ['linear'], ['zoom'],
        style.widthStops[0], style.widthStops[1],
        style.widthStops[2], style.widthStops[3],
    ];

    const layers: any[] = [];

    if (withCasing) {
        // Thin halo under the colored line for readability on any basemap.
        layers.push({
            id: `${idPrefix}_casing`,
            type: 'line',
            source: sourceId,
            'source-layer': sourceLayerName,
            paint: {
                'line-color': '#f8fafc', // slate-50
                'line-width': [
                    'interpolate', ['linear'], ['zoom'],
                    style.widthStops[0], style.widthStops[1] + 0.8,
                    style.widthStops[2], style.widthStops[3] + 1.2,
                ],
                'line-opacity': 0.55,
            },
            layout: { 'line-cap': 'round', 'line-join': 'round' },
        });
    }

    const mainPaint: any = {
        'line-color': style.lineColor,
        'line-width': widthExpr,
        'line-opacity': [
            'interpolate', ['linear'], ['zoom'],
            3, 0.5,
            10, 0.9,
        ],
    };
    if (style.dashArray) mainPaint['line-dasharray'] = style.dashArray;

    layers.push({
        id: `${idPrefix}_line`,
        type: 'line',
        source: sourceId,
        'source-layer': sourceLayerName,
        paint: mainPaint,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
    });

    return layers;
}

/** Polygon fill + outline for the resource_potential source. */
export function polygonLayers(args: {
    sourceId: string;
    sourceLayerName: string;
    idPrefix: string;
}): any[] {
    const { sourceId, sourceLayerName, idPrefix } = args;
    return [
        {
            id: `${idPrefix}_fill`,
            type: 'fill',
            source: sourceId,
            'source-layer': sourceLayerName,
            paint: {
                'fill-color': POTENTIAL_FILL_RAMP,
                'fill-opacity': [
                    'interpolate', ['linear'], ['zoom'],
                    0, 0.35,
                    6, 0.5,
                    12, 0.65,
                ],
            },
        },
        {
            id: `${idPrefix}_outline`,
            type: 'line',
            source: sourceId,
            'source-layer': sourceLayerName,
            minzoom: 6,
            paint: {
                'line-color': '#7f1d1d', // red-900
                'line-width': [
                    'interpolate', ['linear'], ['zoom'],
                    6, 0.3,
                    12, 1.0,
                ],
                'line-opacity': 0.7,
            },
        },
    ];
}

// ── Filter expressions ─────────────────────────────────────────────────

/** Build a MapLibre filter by jurisdiction_code (or null for all). */
export function jurisdictionFilter(jurisdictionCode: string | null): any | null {
    if (!jurisdictionCode) return null;
    return ['==', ['get', 'jurisdiction_code'], jurisdictionCode];
}

/**
 * Build a MapLibre filter by commodity_grouping. Applied across all point
 * layers so selecting "Gold" (precious_metals grouping) filters mines,
 * occurrences, and drillholes consistently (plan §09a, right-rail commodity
 * picker).
 */
export function commodityGroupingFilter(grouping: string | null): any | null {
    if (!grouping) return null;
    return ['==', ['get', 'commodity_grouping'], grouping];
}

/** Combine an arbitrary number of filter expressions with `all`. */
export function combineFilters(...filters: (any | null)[]): any | null {
    const active = filters.filter(Boolean);
    if (active.length === 0) return null;
    if (active.length === 1) return active[0];
    return ['all', ...active];
}

/** Zoom thresholds exported for reuse in the component / tests. */
export const ZOOM_THRESHOLDS = {
    HEATMAP_MAX,
    CIRCLES_MIN,
    LABEL_MIN,
} as const;
