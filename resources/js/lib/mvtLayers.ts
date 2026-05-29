/**
 * MVT layer registry for MapView.
 *
 * All silver (per-project) tile layers are defined here. Each entry maps
 * a logical layer id to the full Martin function name, the ST_AsMVT layer
 * name (confirmed from the SQL migration source), and the MapLibre layer
 * style spec.
 *
 * Source of truth for function names and sourceLayer strings:
 *   database/migrations/2026_04_22_130000_create_silver_mvt_functions.php
 *   database/migrations/2026_04_22_140000_create_silver_boundary_formation_working_geochem.php
 *
 * DO NOT edit sourceLayer strings without re-confirming the ST_AsMVT literal
 * inside the corresponding PostgreSQL function.
 *
 * Module 8 Chunks 8.5 + 8.6.
 */

export interface MvtLayerOutline {
    paint: {
        'line-color': string;
        'line-width': number;
        'line-opacity': number;
    };
}

export interface MvtLayerDef {
    /** MapLibre layer id prefix — becomes `mvt-<id>` and `mvt-<id>-source`. */
    id: string;
    /** Human-readable label for the layer toggle UI (Title Case). */
    label: string;
    /** Full Martin function name — used in the tile URL path segment. */
    functionName: string;
    /**
     * ST_AsMVT layer name — must match the literal second argument to
     * ST_AsMVT(..., '<sourceLayer>', ...) in the PostgreSQL function body.
     */
    sourceLayer: string;
    type: 'circle' | 'line' | 'fill';
    minzoom: number;
    maxzoom: number;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    paint: Record<string, any>;
    outline?: MvtLayerOutline;
}

/**
 * Registry of all project-scoped silver MVT layers.
 * Order determines render order (bottom → top).
 */
export const MVT_LAYERS: MvtLayerDef[] = [
    // ── Boundaries — project claim/lease/tenement polygons ─────────────────
    {
        id: 'boundaries',
        label: 'Boundaries',
        functionName: 'pg_boundaries_by_project',
        sourceLayer: 'boundaries',          // ST_AsMVT(tile, 'boundaries', 4096, 'geom')
        type: 'fill',
        minzoom: 0,
        maxzoom: 16,
        paint: {
            'fill-color': '#6366f1',
            'fill-opacity': 0.12,
        },
        outline: {
            paint: { 'line-color': '#6366f1', 'line-width': 2, 'line-opacity': 0.7 },
        },
    },

    // ── Formations — mapped geological formation polygons ──────────────────
    {
        id: 'formations',
        label: 'Formations',
        functionName: 'pg_formations_by_project',
        sourceLayer: 'formations',          // ST_AsMVT(tile, 'formations', 4096, 'geom')
        type: 'fill',
        minzoom: 0,
        maxzoom: 16,
        paint: {
            'fill-color': '#f97316',
            'fill-opacity': 0.1,
        },
        outline: {
            paint: { 'line-color': '#f97316', 'line-width': 1.5, 'line-opacity': 0.6 },
        },
    },

    // ── Seismic survey footprints — bbox polygons ───────────────────────────
    // Pre-approved V1 item (2026-04-20). Polygon bbox geometry.
    // sourceLayer confirmed: ST_AsMVT(tile, 'seismic', 4096, 'geom')
    // in silver.pg_seismic_by_project (2026_04_22_130000 migration).
    {
        id: 'seismic',
        label: 'Seismic',
        functionName: 'pg_seismic_by_project',
        sourceLayer: 'seismic',             // ST_AsMVT(tile, 'seismic', 4096, 'geom')
        type: 'fill',
        minzoom: 4,
        maxzoom: 16,
        paint: {
            'fill-color': '#0ea5e9',         // sky-500 — distinct from boundaries (indigo) and formations (orange)
            'fill-opacity': 0.18,
        },
        outline: {
            paint: { 'line-color': '#0ea5e9', 'line-width': 1.5, 'line-opacity': 0.7 },
        },
    },

    // ── Drill traces — LineStringZ traces ──────────────────────────────────
    {
        id: 'traces',
        label: 'Drill traces',
        functionName: 'pg_drill_traces_by_project',
        sourceLayer: 'drill_traces',        // ST_AsMVT(tile, 'drill_traces', 4096, 'geom')
        type: 'line',
        minzoom: 4,
        maxzoom: 16,
        paint: {
            'line-color': '#3b82f6',
            'line-width': ['interpolate', ['linear'], ['zoom'], 6, 0.5, 10, 1.5, 14, 3],
            'line-opacity': 0.8,
        },
    },

    // ── Historic workings — point locations ────────────────────────────────
    {
        id: 'historic-workings',
        label: 'Historic workings',
        functionName: 'pg_historic_workings_by_project',
        sourceLayer: 'historic_workings',   // ST_AsMVT(tile, 'historic_workings', 4096, 'geom')
        type: 'circle',
        minzoom: 6,
        maxzoom: 16,
        paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 2, 14, 6],
            'circle-color': '#a855f7',
            'circle-stroke-width': 1,
            'circle-stroke-color': '#ffffff',
            'circle-opacity': 0.8,
        },
    },

    // ── Geochemistry samples — point locations ─────────────────────────────
    // Pre-approved V1 item (2026-04-20). Individual sample locations.
    // Hidden at low zoom (minzoom: 8) — too dense at regional scale.
    // sourceLayer confirmed: ST_AsMVT(tile, 'geochem', 4096, 'geom')
    // in silver.pg_geochem_by_project (2026_04_22_140000 migration).
    {
        id: 'geochem',
        label: 'Geochem samples',
        functionName: 'pg_geochem_by_project',
        sourceLayer: 'geochem',             // ST_AsMVT(tile, 'geochem', 4096, 'geom')
        type: 'circle',
        minzoom: 8,                         // hide at low zoom — points are too dense
        maxzoom: 16,
        paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 1.5, 12, 3, 16, 5],
            'circle-color': '#84cc16',       // lime-500
            'circle-stroke-width': 0.5,
            'circle-stroke-color': '#ffffff',
            'circle-opacity': 0.85,
        },
    },

    // ── Drill collars — point locations (rendered on top) ──────────────────
    {
        id: 'collars',
        label: 'Collars',
        functionName: 'pg_collars_by_project',
        sourceLayer: 'collars',             // ST_AsMVT(tile, 'collars', 4096, 'geom')
        type: 'circle',
        minzoom: 0,
        maxzoom: 16,
        paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 1.5, 8, 3, 12, 6, 16, 10],
            'circle-color': [
                'match', ['get', 'status'],
                'Completed', '#22c55e',
                'Active', '#eab308',
                'Abandoned', '#ef4444',
                '#6b7280',
            ],
            'circle-stroke-width': 1.5,
            'circle-stroke-color': '#ffffff',
            'circle-opacity': 0.9,
        },
    },
];

/**
 * Layer ids that respond to click and hover interactions.
 * Prefixed with `mvt-` to match the MapLibre layer id pattern used in MapView.
 *
 * Seismic and geochem are both interactive — geologists need click popups
 * to inspect survey metadata and sample assay codes.
 */
export const MVT_INTERACTIVE_LAYERS: string[] = [
    'mvt-collars',
    'mvt-historic-workings',
    'mvt-seismic',
    'mvt-geochem',
];

/**
 * Default visibility state for all MVT layers.
 * Seismic and geochem default to true — geologists want to see them immediately.
 */
export const MVT_DEFAULT_VISIBILITY: Record<string, boolean> = {
    boundaries: true,
    formations: true,
    seismic: true,
    traces: true,
    'historic-workings': true,
    geochem: true,
    collars: true,
};
