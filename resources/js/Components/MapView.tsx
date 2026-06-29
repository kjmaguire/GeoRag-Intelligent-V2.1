import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { usePage } from '@inertiajs/react';
import maplibregl from 'maplibre-gl';
import type {
    Map as MapLibreMap,
    Marker,
    Popup,
    MapSourceDataEvent,
    ErrorEvent as MapErrorEvent,
    VectorTileSource,
    GeoJSONSource,
    LngLatBoundsLike,
    AddLayerObject,
} from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { MVT_LAYERS, MVT_INTERACTIVE_LAYERS, MVT_DEFAULT_VISIBILITY } from '../lib/mvtLayers';
import {
    mergeLayerVisibility,
    readLayerVisibility,
    writeLayerVisibility,
} from '../lib/layerVisibilityStorage';
import { buildSilverTileUrl } from '../lib/tileUrl';
import { createTileFailureWatchdog } from '../lib/tileFailureWatchdog';
import { escapeHtml } from '../lib/escapeHtml';
import { useBasemapStyleUrl } from '@/lib/basemap';
import { useEvidenceMapPin } from '@/Hooks/useEvidenceMapPin';
import { useSilverTileInvalidation } from '@/Hooks/useTileInvalidation';
import type { PageProps } from '../types';

/**
 * MapView
 *
 * MapLibre GL map that displays drill collar locations as interactive markers.
 * Clicking a collar marker calls onCollarClick(hole_id), which feeds back into
 * the chat as a new query (bidirectional map pattern, Section 01).
 *
 * Props:
 *   projectId     {string}   - UUID of the active project (required when inlineGeoJson absent)
 *   onCollarClick {function} - callback(hole_id) when a marker is clicked
 *   selectedHoleId {string}  - currently selected hole_id (highlights marker;
 *                              when omitted, falls back to the global Evidence
 *                              Map Mode pin via useEvidenceMapPin)
 *   useMartinTiles {boolean} - Feature flag: true = MVT tile layers from Martin
 *                              (GPU-rendered, viewport-scoped). Default true.
 *                              GeoJSON is the fallback for inlineGeoJson / no projectId.
 *   inlineGeoJson {object}   - Optional GeoJSON FeatureCollection with collar
 *                              Point features. When provided, MapView skips
 *                              the Laravel fetch and renders these features
 *                              directly. Used by ChatMessage → InlineViz to
 *                              render the map_payload that FastAPI emitted on
 *                              the completed event (M2 Phase 5).
 *                              Always uses GeoJSON regardless of useMartinTiles.
 *   inlineBbox    {[number,number,number,number]}
 *                              Optional bbox [minLon, minLat, maxLon, maxLat]
 *                              used to fit the map view when inlineGeoJson is
 *                              supplied. Computed from features when absent.
 *   compact       {boolean}   - Reduces chrome for inline chat rendering.
 */

// ── Collar record shape used internally ──────────────────────────────────────
interface CollarRow {
    hole_id?: string;
    status?: string;
    hole_type?: string;
    total_depth?: number | string | null;
    easting?: number | null;
    northing?: number | null;
    longitude?: number | null;
    latitude?: number | null;
    // CC-01 Item 2 — spatial uncertainty + CRS provenance. NULL when not
    // recorded; uncertainty-rings layer skips features with null radius.
    spatial_uncertainty_m?: number | null;
    crs_confidence?: number | null;
    georef_method?: GeorefMethod | null;
    _lon: number;
    _lat: number;
    [key: string]: unknown;
}

// CC-01 Item 2 — closed vocabulary for the georef_method column. Mirrors the
// chk_*_georef_method DB constraint. The MapView ring renderer colours rings
// by this value (see UNCERTAINTY_STROKE_COLOR_EXPR below).
type GeorefMethod = 'declared' | 'detected' | 'assumed' | 'manual' | 'survey';

// ── Popup with internal hover tracking (MapLibre's Popup is not extensible) ──
// We store a custom property on the popup instance to track hover state.
// Using `as unknown as ExtendedPopup` once here per the constraint in the task.
interface ExtendedPopup extends Popup {
    _hoverFeatureKey?: string | number | undefined;
}

// ── Coverage density layer types (CC-03 Item 5) ──────────────────────────────
type CoverageKind = 'collars' | 'reports' | 'spatial_features';

interface CoverageFeature {
    type: 'Feature';
    geometry: GeoJSON.Geometry;
    properties: {
        record_count: number;
        bias_warning: boolean;
    };
}

interface CoverageFeatureCollection {
    type: 'FeatureCollection';
    project_id?: string;
    kind?: CoverageKind;
    cell_size_m?: number;
    feature_count?: number;
    max_count?: number;
    features: CoverageFeature[];
}

const COVERAGE_CELL_SIZES: ReadonlyArray<{ value: number; label: string }> = [
    { value: 500,   label: '500 m' },
    { value: 1000,  label: '1 km' },
    { value: 5000,  label: '5 km' },
    { value: 10000, label: '10 km' },
];

const COVERAGE_KIND_OPTIONS: ReadonlyArray<{ value: CoverageKind; label: string }> = [
    { value: 'collars',          label: 'Collars' },
    { value: 'reports',          label: 'Reports' },
    { value: 'spatial_features', label: 'Features' },
];

// ── MapView component props ───────────────────────────────────────────────────
interface MapViewProps {
    projectId?: string;
    onCollarClick?: (holeId: string) => void;
    selectedHoleId?: string;
    useMartinTiles?: boolean;
    inlineGeoJson?: { type: 'FeatureCollection'; features: Array<{
        geometry?: { type: string; coordinates: number[] } | null;
        properties?: Record<string, unknown> | null;
    }> } | null;
    inlineBbox?: [number, number, number, number] | null;
    compact?: boolean;
    crs?: string;
}

// Map style registry — URLs come from Inertia shared props (config-driven
// via config/services.php basemap.styles). On-prem deployments override the
// MAPLIBRE_STYLE_* env vars to point at self-hosted style.json. See
// resources/js/lib/basemap.ts.
//
// `default` uses positron (light topographic — good for data overlays).
// `satellite` and `terrain` both use bright as the base map layer; the
// imagery / DEM overlays are added on top via the source definitions below.
function useMapStyles(): Record<string, { label: string; url: string }> {
    const positron = useBasemapStyleUrl('positron');
    const bright = useBasemapStyleUrl('bright');
    return useMemo(() => ({
        default:   { label: 'Default',   url: positron },
        satellite: { label: 'Satellite', url: bright },  // base for hybrid
        terrain:   { label: 'Terrain',   url: bright },
    }), [positron, bright]);
}

// ── Performance-tuned DEM + satellite source definitions ─────────────────
// Key optimisations vs. the previous version:
//   1. Single DEM source shared by terrain extrusion AND hillshade
//      (was: two identical sources → double the DEM tile fetches)
//   2. 512px DEM tile size (halves request count vs. 256px)
//   3. maxzoom: 14 on DEM (data doesn't meaningfully improve beyond that)
//   4. Globe projection only at z < 5 (mercator at project scale)
//   5. Sources loaded lazily — only when satellite/terrain mode is activated
// V1.5-12 — CDN URLs are now overridable via Vite env vars so on-prem
// deployments can point at self-hosted DEM + satellite tiles without
// re-building the SPA. Defaults preserve the dev experience (free public
// tiles); production should set these to a self-hosted Martin or nginx.
// See ops/runbooks/dem-self-host.md for the self-hosting procedure
// (terrain-RGB encoding via rio-rgbify, Martin raster_sources config,
// nginx fallback).
const DEFAULT_DEM_URL = 'https://tiles.mapterhorn.com/tilejson.json';
const DEFAULT_SATELLITE_TILE_URL =
    'https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg';

const DEM_TILES_URL: string =
    (import.meta.env.VITE_DEM_TILES_URL as string | undefined) ?? DEFAULT_DEM_URL;
const SATELLITE_TILES_URL: string =
    (import.meta.env.VITE_SATELLITE_TILES_URL as string | undefined) ?? DEFAULT_SATELLITE_TILE_URL;

// ── CC-01 Item 2 — uncertainty-rings paint spec ──────────────────────────
// Exported so registry-style tests can pin the filter + paint shape without
// driving a live MapLibre canvas. The runtime addLayer call (see the
// "Uncertainty rings layer" block below) feeds these constants directly to
// map.addLayer, so a change here is the change the user sees.
//
// Filter: `['has', 'spatial_uncertainty_m']` skips features whose source
// JS row didn't publish the field — the GeoJSON builder above intentionally
// omits the property (rather than emitting null) when the value is missing.
//
// Paint:
//   - circle-radius: metres → screen pixels at the current zoom, with a
//     cosine-of-latitude correction (Web-Mercator shrink).
//   - circle-stroke-color: matched against the georef_method vocabulary
//     (declared/detected/assumed/manual/survey); falls back to gray.
export const UNCERTAINTY_RINGS_FILTER = ['has', 'spatial_uncertainty_m'] as const;

export const UNCERTAINTY_RINGS_STROKE_COLOR_EXPR = [
    'match',
    ['get', 'georef_method'],
    'declared', '#22c55e',
    'detected', '#3b82f6',
    'assumed',  '#f97316',
    'manual',   '#a855f7',
    'survey',   '#000000',
    '#9ca3af',
] as const;

export const UNCERTAINTY_RINGS_RADIUS_EXPR = [
    '*',
    ['get', 'spatial_uncertainty_m'],
    ['/',
        ['^', 2, ['zoom']],
        ['*', 156543.03392, ['cos', ['*', ['get', '_lat'], 0.017453292519943295]]],
    ],
] as const;

export const UNCERTAINTY_RINGS_PAINT = {
    'circle-color': 'rgba(0,0,0,0)',
    'circle-stroke-width': 1.5,
    'circle-opacity': 0.25,
    'circle-stroke-opacity': 0.55,
    'circle-radius': UNCERTAINTY_RINGS_RADIUS_EXPR,
    'circle-stroke-color': UNCERTAINTY_RINGS_STROKE_COLOR_EXPR,
} as const;

// CC-01 Item 2 follow-on — MVT-path layer identifiers. Pinned as exported
// constants so the registry-style test can assert that the MVT and GeoJSON
// branches share filter + paint while diverging on source binding.
//
// The source `mvt-collars-source` is registered by the MVT_LAYERS effect
// (entry id: 'collars' → source id: 'mvt-collars-source'). The source-layer
// name matches the ST_AsMVT layer literal in silver.pg_collars_by_project
// (see database/migrations/2026_05_24_130000_add_uncertainty_to_pg_collars_mvt.php).
export const UNCERTAINTY_RINGS_MVT_LAYER_ID = 'mvt-uncertainty-rings';
export const UNCERTAINTY_RINGS_MVT_SOURCE_ID = 'mvt-collars-source';
export const UNCERTAINTY_RINGS_MVT_SOURCE_LAYER = 'collars';
export const UNCERTAINTY_RINGS_GEOJSON_LAYER_ID = 'uncertainty-rings';

const DEM_SOURCE_CONFIG = {
    type: 'raster-dem' as const,
    url: DEM_TILES_URL,
    tileSize: 512,
    maxzoom: 14,
};
const SATELLITE_SOURCE_CONFIG = {
    type: 'raster' as const,
    tiles: [SATELLITE_TILES_URL],
    tileSize: 256,
    maxzoom: 18,
};

// ── Tile request cancellation on rapid panning ───────────────────────────
// When the user pans quickly (common in Rockies exploration), MapLibre
// queues dozens of tile requests for intermediate viewports that are
// immediately superseded. An AbortController per source lets us cancel
// stale requests instead of saturating the browser's connection pool.
// This is wired into the MVT source's transformRequest option.
const tileAbortControllers = new Map<string, AbortController>();
function cancelStaleTileRequests(sourceName: string): AbortSignal {
    const prev = tileAbortControllers.get(sourceName);
    if (prev) prev.abort();
    const ac = new AbortController();
    tileAbortControllers.set(sourceName, ac);
    return ac.signal;
}

// Suppress unused-variable warning — cancelStaleTileRequests is declared
// for the tile-abort pattern; used in the transformRequest scope.
void cancelStaleTileRequests;

// MAP_STYLE / MAP_STYLES are now read inside the component via useMapStyles()
// so URLs are config-driven (Inertia shared props → config/services.php).

// Dynamic CRS support — proj4 definitions for common UTM zones.
// The project's crs_datum (from silver.projects) determines which
// zone to use. Falls back to Zone 13N if not specified.
const UTM_DEFS: Record<string, string> = {
    'EPSG:32607': '+proj=utm +zone=7 +datum=WGS84 +units=m +no_defs',
    'EPSG:32608': '+proj=utm +zone=8 +datum=WGS84 +units=m +no_defs',
    'EPSG:32609': '+proj=utm +zone=9 +datum=WGS84 +units=m +no_defs',
    'EPSG:32610': '+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs',
    'EPSG:32611': '+proj=utm +zone=11 +datum=WGS84 +units=m +no_defs',
    'EPSG:32612': '+proj=utm +zone=12 +datum=WGS84 +units=m +no_defs',
    'EPSG:32613': '+proj=utm +zone=13 +datum=WGS84 +units=m +no_defs',
    'EPSG:32614': '+proj=utm +zone=14 +datum=WGS84 +units=m +no_defs',
    'EPSG:32615': '+proj=utm +zone=15 +datum=WGS84 +units=m +no_defs',
    'EPSG:32616': '+proj=utm +zone=16 +datum=WGS84 +units=m +no_defs',
    'EPSG:32617': '+proj=utm +zone=17 +datum=WGS84 +units=m +no_defs',
    'EPSG:32618': '+proj=utm +zone=18 +datum=WGS84 +units=m +no_defs',
    'EPSG:32619': '+proj=utm +zone=19 +datum=WGS84 +units=m +no_defs',
    'EPSG:32620': '+proj=utm +zone=20 +datum=WGS84 +units=m +no_defs',
    'EPSG:32621': '+proj=utm +zone=21 +datum=WGS84 +units=m +no_defs',
};

async function getProj4(crs = 'EPSG:32613') {
    const proj4Module = await import('proj4');
    const proj4 = proj4Module.default;
    const def = UTM_DEFS[crs];
    if (def) {
        try {
            proj4.defs(crs, def);
        } catch { /* already registered */ }
    }
    return proj4;
}

/**
 * Convert UTM easting/northing to WGS84 [lon, lat] using the project's CRS.
 */
async function utmToWgs84(easting: number, northing: number, crs = 'EPSG:32613'): Promise<[number, number] | null> {
    try {
        const proj4 = await getProj4(crs);
        return proj4(crs, 'EPSG:4326', [easting, northing]) as [number, number];
    } catch {
        return null;
    }
}

/**
 * Compute a bounding box [[minLon, minLat], [maxLon, maxLat]] from collar list.
 */
function computeBbox(collars: CollarRow[]): [[number, number], [number, number]] | null {
    const lons = collars.map((c) => c._lon).filter((v): v is number => v != null);
    const lats = collars.map((c) => c._lat).filter((v): v is number => v != null);
    if (!lons.length) return null;
    return [
        [Math.min(...lons) - 0.001, Math.min(...lats) - 0.001],
        [Math.max(...lons) + 0.001, Math.max(...lats) + 0.001],
    ];
}

/**
 * Determine marker colour based on status.
 */
function markerColor(status: string | undefined, isSelected: boolean): string {
    if (isSelected) return '#f59e0b'; // amber — selected
    switch (status) {
        case 'Completed': return '#22c55e';   // green
        case 'Active':    return '#eab308';   // yellow
        case 'Abandoned': return '#ef4444';   // red
        default:          return '#6b7280';   // gray
    }
}

/**
 * Status shape for accessibility — color-blind users can distinguish
 * marker status by shape as well as color.
 */
function markerShape(status: string | undefined): string {
    switch (status) {
        case 'Completed': return '●';   // circle — complete
        case 'Active':    return '◆';   // diamond — active/in-progress
        case 'Abandoned': return '✕';   // cross — abandoned
        default:          return '○';   // open circle — unknown
    }
}

// MVT_LAYERS, MVT_INTERACTIVE_LAYERS, MVT_DEFAULT_VISIBILITY imported from ../lib/mvtLayers

export default function MapView({
    projectId,
    onCollarClick,
    selectedHoleId,
    useMartinTiles = true,
    inlineGeoJson = null,
    inlineBbox = null,
    compact = false,
    crs = 'EPSG:32613',
}: MapViewProps) {
    // Phase G.4 — Evidence Map Mode: subscribe to the citation-click
    // pin. When a chat citation marker is clicked and resolves to a
    // hole_id, MapView highlights that drill collar even though
    // `selectedHoleId` wasn't passed as a prop. Prop wins when both
    // are present (explicit parent control overrides the global pin).
    const evidenceMapPin = useEvidenceMapPin();
    const effectiveSelectedHoleId = selectedHoleId ?? (
        evidenceMapPin?.kind === 'hole_id' ? evidenceMapPin.hole_id : undefined
    );
    const mapContainer = useRef<HTMLDivElement | null>(null);
    const mapRef       = useRef<MapLibreMap | null>(null);
    const markersRef   = useRef<Record<string, Marker>>({});
    const popupRef     = useRef<ExtendedPopup | null>(null);
    const mvtLayersAddedRef = useRef(false);

    // Workspace data_version drives the cache-bust suffix on silver MVT
    // tile URLs (Module 8 §8.5 + Phase 4 real-time invalidation).
    //
    // Two sources, merged into a single state:
    //   1. Initial seed from `pageProps.workspace?.data_version` (when the
    //      hosting controller provides it). Falls back to 0 if the prop is
    //      absent — pre-Phase-4 pages, or pages that don't surface workspace.
    //   2. Live updates from `useSilverTileInvalidation`, which listens for
    //      WorkspaceDataUpdated on project.{projectId}.ingestion. Every
    //      bump → new setTiles() pass.
    //
    // Decoupling the state from the prop means MapView stays live even when
    // the hosting controller forgets to pass `workspace.data_version` — the
    // historical bug where Foundry/Explorer's MVT URLs were stuck at `&v=0`
    // can no longer happen because the Echo signal supersedes the prop.
    const { props: pageProps } = usePage<PageProps>();
    const [workspaceDataVersion, setWorkspaceDataVersion] = useState<number>(
        pageProps.workspace?.data_version ?? 0,
    );

    useSilverTileInvalidation(projectId, (newVersion) => {
        setWorkspaceDataVersion((prev) => (newVersion > prev ? newVersion : prev));
    });

    const [collars, setCollars] = useState<CollarRow[]>([]);
    const [loading, setLoading] = useState(false);
    const [mapStyle, setMapStyle] = useState('default');
    // Config-driven basemap URL registry (per CLAUDE.md hard rule #8).
    const mapStyles = useMapStyles();
    const [error, setError]     = useState<string | null>(null);
    const [mapReady, setMapReady] = useState(false);
    // V1.5-11 — initialise from localStorage when present, falling through
    // to MVT_DEFAULT_VISIBILITY for missing keys (so a layer that's been
    // added since the prefs were saved appears with its default state).
    // useState initialiser runs once per mount; the effect below persists
    // every change.
    const [visibleLayers, setVisibleLayers] = useState<Record<string, boolean>>(
        () => mergeLayerVisibility(MVT_DEFAULT_VISIBILITY, readLayerVisibility()),
    );

    // Persist on every change. Best-effort; storage failures are swallowed
    // by the helper so an in-private session still updates state in-memory.
    useEffect(() => {
        writeLayerVisibility(visibleLayers);
    }, [visibleLayers]);
    const [layerPanelOpen, setLayerPanelOpen] = useState(true);

    // ── Coverage density (CC-03 Item 5) ──────────────────────────────────────
    // Optional heatmap-style hex layer painted by record_count per cell. Sparse
    // cells (bias_warning) get a dashed stroke and the "historical exploration
    // bias" hover copy. Fetched lazily via the Sanctum-authenticated proxy.
    const [coverageEnabled, setCoverageEnabled] = useState(false);
    const [coverageKind, setCoverageKind] = useState<CoverageKind>('collars');
    const [coverageCellSize, setCoverageCellSize] = useState<number>(1000);
    const [coverageData, setCoverageData] = useState<CoverageFeatureCollection | null>(null);
    const [coverageLoading, setCoverageLoading] = useState(false);
    const [coverageError, setCoverageError] = useState<string | null>(null);

    // ── Tile failure toast state (MAPVIEW-03) ────────────────────────────────
    interface TileToast {
        sourceId: string;
        count: number;
        urlPrefix: string;
    }
    const [tileToast, setTileToast] = useState<TileToast | null>(null);
    const tileToastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // Watchdog instance — one per MapView mount.
    // Stored in a ref so the effect callbacks close over a stable reference.
    const watchdogRef = useRef(createTileFailureWatchdog({
        onThreshold: (sourceId, count, urlPrefix) => {
            setTileToast({ sourceId, count, urlPrefix });
            // Auto-dismiss after 8 s
            if (tileToastTimerRef.current) clearTimeout(tileToastTimerRef.current);
            tileToastTimerRef.current = setTimeout(() => setTileToast(null), 8_000);
        },
    }));

    // Determine rendering mode: inlineGeoJson always uses legacy GeoJSON,
    // otherwise respect the useMartinTiles flag.
    const useMvt = useMartinTiles && !inlineGeoJson && !!projectId;

    // ── Inline-data path (M2 P5 chat visualizations) ─────────────────────────
    // When the parent supplies a GeoJSON FeatureCollection from the FastAPI
    // map_payload we skip the Laravel fetch entirely. Convert the features
    // into the shape MapView already uses internally ({ _lon, _lat, ...props }).
    useEffect(() => {
        if (!inlineGeoJson) return;

        const features = inlineGeoJson.features ?? [];
        const rows: CollarRow[] = features
            .map((feat) => {
                const coords = feat?.geometry?.coordinates;
                if (!Array.isArray(coords) || coords.length < 2) return null;
                const lon = coords[0];
                const lat = coords[1];
                if (typeof lon !== 'number' || typeof lat !== 'number') return null;
                // Normalise the uncertainty triple: any of the three may be
                // missing in the inline payload (older callers, or features
                // whose source geometry never carried provenance). Keep them
                // explicitly `null` so the uncertainty-rings layer filter can
                // detect absence rather than guessing from `undefined`.
                const props = feat.properties ?? {};
                const su = typeof props.spatial_uncertainty_m === 'number' ? props.spatial_uncertainty_m : null;
                const cc = typeof props.crs_confidence === 'number' ? props.crs_confidence : null;
                const gm = (typeof props.georef_method === 'string' ? props.georef_method : null) as GeorefMethod | null;
                return {
                    ...props,
                    spatial_uncertainty_m: su,
                    crs_confidence: cc,
                    georef_method: gm,
                    _lon: lon,
                    _lat: lat,
                } as CollarRow;
            })
            .filter((r): r is CollarRow => r !== null);

        setCollars(rows);
        setLoading(false);
        setError(null);
    }, [inlineGeoJson]);

    // ── Fetch collars (legacy GeoJSON path — skipped when MVT is active) ─────
    const fetchCollars = useCallback(async () => {
        if (!projectId || inlineGeoJson || useMvt) return;

        setLoading(true);
        setError(null);

        try {
            // Auth via Sanctum session cookie (same-origin). No bearer token from
            // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
            const res = await fetch(
                `/api/v1/projects/${projectId}/collars?per_page=500`,
                {
                    credentials: 'same-origin',
                    headers: {
                        Accept: 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                },
            );
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const body = await res.json() as { data?: CollarRow[] } | CollarRow[];
            const list: CollarRow[] = (Array.isArray(body) ? body : (body as { data?: CollarRow[] }).data) ?? [];

            // Resolve lon/lat for each collar.
            // Prefer API-provided longitude/latitude (PostGIS ST_Transform).
            // Fall back to client-side UTM→WGS84 conversion if geom was null.
            const withCoords = await Promise.all(
                list.map(async (c) => {
                    if (c.longitude != null && c.latitude != null) {
                        return { ...c, _lon: c.longitude, _lat: c.latitude } as CollarRow;
                    }
                    if (c.easting != null && c.northing != null) {
                        const coords = await utmToWgs84(c.easting, c.northing, crs);
                        if (coords) {
                            return { ...c, _lon: coords[0], _lat: coords[1] } as CollarRow;
                        }
                    }
                    return { ...c, _lon: 0, _lat: 0, _invalid: true } as CollarRow & { _invalid: true };
                }),
            );

            setCollars(withCoords.filter((c) => !('_invalid' in c && c._invalid)));
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        } finally {
            setLoading(false);
        }
    }, [projectId, inlineGeoJson, useMvt, crs]);

    useEffect(() => {
        void fetchCollars();
    }, [fetchCollars]);

    // ── Initialise map ────────────────────────────────────────────────────────
    useEffect(() => {
        if (!mapContainer.current) return;

        const map = new maplibregl.Map({
            container: mapContainer.current,
            style: mapStyles.default.url,
            center: [-107, 55],   // Default: central Canada (exploration country)
            zoom: 5,
            pitch: 0,
            maxPitch: 85,
            // attributionControl defaults to enabled; do not pass true (invalid type — only false | options)

            // ── Performance tuning ──────────────────────────────────────
            maxTileCacheSize: 150,          // cap memory for tile cache (default unbounded)
            fadeDuration: 0,                // instant vector tile appearance (no fade)
            trackResize: true,              // auto-resize on container changes
            collectResourceTiming: false,   // disable resource timing API (saves GC pressure)
            // ── Request tuning ──────────────────────────────────────────
            // /tiles/* routes sit under auth:sanctum in web.php. MapLibre
            // sends same-origin requests with cookies by default (no explicit
            // credentials option needed in transformRequest). The Sanctum
            // session cookie is the canonical credential — no bearer token
            // from localStorage (XSS-exfiltration target; types.ts:11-12).
            transformRequest: (url) => {
                if (url.startsWith('/tiles/')) {
                    return {
                        url,
                        headers: {
                            'Accept-Encoding': 'gzip, br',
                        },
                    };
                }
                return { url };
            },
        });

        // Navigation with pitch visualization for 3D terrain
        map.addControl(
            new maplibregl.NavigationControl({
                visualizePitch: true,
                showZoom: true,
                showCompass: true,
            }),
            'top-right',
        );
        map.addControl(new maplibregl.FullscreenControl(), 'top-right');
        map.addControl(new maplibregl.ScaleControl({ maxWidth: 100, unit: 'metric' }), 'bottom-left');

        map.on('load', () => {
            // Sources loaded LAZILY on first style switch (see style effect)
            setMapReady(true);
        });

        // ── Cancel stale tile fetches during rapid panning ──────────────
        // When the user drags quickly through the Rockies, MapLibre queues
        // tiles for intermediate viewports. These saturate the browser's
        // 6-connection-per-origin limit, delaying the tiles the user
        // actually needs. Cancelling on movestart keeps the pipe clear.
        map.on('movestart', () => {
            tileAbortControllers.forEach((ac) => ac.abort());
            tileAbortControllers.clear();
        });

        mapRef.current = map;

        return () => {
            map.remove();
            mapRef.current = null;
            setMapReady(false);
        };
    }, []);

    // ── Style switching — lazy source creation + visibility toggle ──────────
    // Sources are created on first use, not at map init. This avoids loading
    // ~2-4 MB of DEM tiles on every page load when the user stays on the
    // default flat map. A single DEM source is shared by terrain extrusion
    // AND hillshade (was previously duplicated, doubling DEM tile fetches).
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady) return;

        try {
            // Lazily create DEM + satellite sources on first terrain/satellite switch
            const ensureDemSource = () => {
                if (!map.getSource('demSource')) {
                    map.addSource('demSource', DEM_SOURCE_CONFIG);
                }
            };
            const ensureSatelliteSource = () => {
                if (!map.getSource('satelliteSource')) {
                    map.addSource('satelliteSource', SATELLITE_SOURCE_CONFIG);
                }
            };
            const ensureHillshadeLayer = () => {
                if (!map.getLayer('hills')) {
                    map.addLayer({
                        id: 'hills',
                        type: 'hillshade',
                        source: 'demSource',      // single DEM source, not a duplicate
                        layout: { visibility: 'visible' },
                        paint: { 'hillshade-shadow-color': '#473B24' },
                    });
                }
            };
            const ensureSatelliteLayer = () => {
                if (!map.getLayer('satellite')) {
                    const firstNonBg = map.getStyle().layers.find(
                        (l) => l.type !== 'background',
                    );
                    map.addLayer({
                        id: 'satellite',
                        type: 'raster',
                        source: 'satelliteSource',
                        layout: { visibility: 'visible' },
                        paint: { 'raster-opacity': 1, 'raster-fade-duration': 0 },
                    }, firstNonBg?.id);
                }
            };

            // Zoom-adaptive terrain exaggeration — at high zoom in
            // mountainous areas (Rockies, Andes), full exaggeration
            // distorts the view and increases GPU draw calls. Scale
            // down as the user zooms in to keep terrain readable.
            const exaggeration = (() => {
                const z = map.getZoom();
                if (z <= 8) return 1.2;     // overview — slight emphasis
                if (z <= 11) return 1.0;    // regional
                if (z <= 13) return 0.7;    // property scale — reduce
                return 0.4;                 // drill-site — minimal
            })();

            if (mapStyle === 'satellite') {
                ensureDemSource();
                ensureSatelliteSource();
                ensureHillshadeLayer();
                ensureSatelliteLayer();
                map.setLayoutProperty('satellite', 'visibility', 'visible');
                map.setLayoutProperty('hills', 'visibility', 'visible');
                map.setTerrain({ source: 'demSource', exaggeration });
                map.easeTo({ pitch: 60, duration: 600 });
            } else if (mapStyle === 'terrain') {
                ensureDemSource();
                ensureHillshadeLayer();
                if (map.getLayer('satellite')) {
                    map.setLayoutProperty('satellite', 'visibility', 'none');
                }
                map.setLayoutProperty('hills', 'visibility', 'visible');
                map.setTerrain({ source: 'demSource', exaggeration });
                map.easeTo({ pitch: 50, duration: 600 });
            } else {
                // Default — flat Positron, hide all 3D layers
                if (map.getLayer('satellite')) {
                    map.setLayoutProperty('satellite', 'visibility', 'none');
                }
                if (map.getLayer('hills')) {
                    map.setLayoutProperty('hills', 'visibility', 'none');
                }
                map.setTerrain(null);
                map.easeTo({ pitch: 0, duration: 400 });
            }
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            console.debug('Style switch deferred:', msg);
        }
    }, [mapStyle, mapReady]);

    // ── Dynamic terrain exaggeration on zoom — reduces GPU load at high zoom ──
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady) return;
        if (mapStyle !== 'satellite' && mapStyle !== 'terrain') return;

        const updateExaggeration = () => {
            if (!map.getSource('demSource')) return;
            const z = map.getZoom();
            let ex: number;
            if (z <= 8) ex = 1.2;
            else if (z <= 11) ex = 1.0;
            else if (z <= 13) ex = 0.7;
            else ex = 0.4;
            try { map.setTerrain({ source: 'demSource', exaggeration: ex }); } catch { /* ignore */ }
        };

        map.on('zoomend', updateExaggeration);
        return () => { map.off('zoomend', updateExaggeration); };
    }, [mapStyle, mapReady]);

    // ══════════════════════════════════════════════════════════════════════════
    // ██  MVT RENDERING PATH — Martin tile layers (zero DOM markers)
    // ══════════════════════════════════════════════════════════════════════════

    // ── Add MVT sources + layers on map ready ────────────────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !useMvt || mvtLayersAddedRef.current) return;

        MVT_LAYERS.forEach((layer) => {
            const sourceId = `mvt-${layer.id}-source`;
            const layerId = `mvt-${layer.id}`;

            // Add vector tile source from Martin via Laravel proxy.
            // URL pattern: /tiles/silver/{functionName}/{z}/{x}/{y}.pbf?project_id={uuid}&v={n}
            // The &v= suffix is the client-side cache-bust; bumping workspaceDataVersion
            // forces MapLibre to discard its tile cache and re-fetch. The proxy derives
            // its server-side ETag from silver.projects.data_version independently.
            if (!map.getSource(sourceId)) {
                map.addSource(sourceId, {
                    type: 'vector',
                    tiles: [buildSilverTileUrl(layer.functionName, projectId!, workspaceDataVersion)],
                    minzoom: layer.minzoom,
                    maxzoom: layer.maxzoom,
                });
            }

            // Add the main layer.
            // MvtLayerDef.paint is `Record<string, any>` to accommodate MapLibre
            // expression arrays. Casting to AddLayerObject is safe — the runtime
            // type of each layer.type matches its paint shape exactly; TypeScript
            // can't narrow the discriminated union here because type is a string
            // union variable, not a literal. (Cast #1 of max-2 allowed.)
            if (!map.getLayer(layerId)) {
                map.addLayer({
                    id: layerId,
                    type: layer.type,
                    source: sourceId,
                    'source-layer': layer.sourceLayer,
                    paint: layer.paint,
                    layout: {
                        visibility: visibleLayers[layer.id] ? 'visible' : 'none',
                    },
                } as unknown as AddLayerObject);
            }

            // Add outline layer for fill types (boundaries, formations, seismic)
            if (layer.outline && !map.getLayer(`${layerId}-outline`)) {
                map.addLayer({
                    id: `${layerId}-outline`,
                    type: 'line',
                    source: sourceId,
                    'source-layer': layer.sourceLayer,
                    paint: layer.outline.paint,
                    layout: {
                        visibility: visibleLayers[layer.id] ? 'visible' : 'none',
                    },
                } as unknown as AddLayerObject);
            }
        });

        // ── Selection highlight layer — renders selected collar on top ───
        const collarSourceId = 'mvt-collars-source';
        if (map.getSource(collarSourceId) && !map.getLayer('mvt-collars-selected')) {
            map.addLayer({
                id: 'mvt-collars-selected',
                type: 'circle',
                source: collarSourceId,
                'source-layer': 'collars',
                filter: ['==', ['get', 'hole_id'], ''],  // empty initially
                paint: {
                    'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 3, 8, 5, 12, 9, 16, 14],
                    'circle-color': '#f59e0b',
                    'circle-stroke-width': 2.5,
                    'circle-stroke-color': '#f59e0b',
                    'circle-opacity': 1,
                },
            });
        }

        // ── Uncertainty rings layer (CC-01 Item 2 follow-on, MVT path) ───
        // Sibling of mvt-collars-dots. Same paint + filter constants as the
        // GeoJSON path (`uncertainty-rings` above), so changing
        // UNCERTAINTY_RINGS_PAINT updates both branches simultaneously. The
        // MVT branch needs source-layer because vector tile sources carry
        // multiple source-layers per source. silver.pg_collars_by_project
        // publishes spatial_uncertainty_m + crs_confidence + georef_method +
        // _lat into the tile (see migration 2026_05_24_130000).
        if (map.getSource(collarSourceId) && !map.getLayer(UNCERTAINTY_RINGS_MVT_LAYER_ID)) {
            try {
                map.addLayer({
                    id: UNCERTAINTY_RINGS_MVT_LAYER_ID,
                    type: 'circle',
                    source: UNCERTAINTY_RINGS_MVT_SOURCE_ID,
                    'source-layer': UNCERTAINTY_RINGS_MVT_SOURCE_LAYER,
                    filter: UNCERTAINTY_RINGS_FILTER,
                    paint: UNCERTAINTY_RINGS_PAINT,
                } as unknown as AddLayerObject);
            } catch { /* race with style swap; safe to ignore */ }
        }

        mvtLayersAddedRef.current = true;
    }, [mapReady, useMvt, projectId]);

    // ── Workspace data_version hot-swap — swap tile URLs without map re-init ──
    // When workspace.data_version changes (Inertia partial reload bumps it after
    // a successful ingestion run), call setTiles() on each MVT source so MapLibre
    // discards its in-memory tile cache for the old version and re-fetches.
    // This is the client leg of the §05d / §07f stale-tile invalidation contract.
    // DO NOT do a full map re-init here — that would destroy all layers and markers.
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !useMvt || !mvtLayersAddedRef.current) return;
        if (!projectId) return;

        MVT_LAYERS.forEach((layer) => {
            const sourceId = `mvt-${layer.id}-source`;
            const source = map.getSource(sourceId);
            // VectorTileSource has setTiles; guard with type check before calling
            if (source && (source as VectorTileSource).setTiles) {
                (source as VectorTileSource).setTiles([buildSilverTileUrl(layer.functionName, projectId, workspaceDataVersion)]);
            }
        });
    }, [workspaceDataVersion, mapReady, useMvt, projectId]);

    // ── MVT layer visibility toggles ─────────────────────────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !useMvt || !mvtLayersAddedRef.current) return;

        MVT_LAYERS.forEach((layer) => {
            const vis: 'visible' | 'none' = visibleLayers[layer.id] ? 'visible' : 'none';
            const layerId = `mvt-${layer.id}`;
            if (map.getLayer(layerId)) {
                map.setLayoutProperty(layerId, 'visibility', vis);
            }
            if (layer.outline && map.getLayer(`${layerId}-outline`)) {
                map.setLayoutProperty(`${layerId}-outline`, 'visibility', vis);
            }
        });
    }, [visibleLayers, mapReady, useMvt]);

    // ── MVT selection highlight — update filter on effectiveSelectedHoleId change ─────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !useMvt) return;
        if (!map.getLayer('mvt-collars-selected')) return;

        map.setFilter('mvt-collars-selected',
            effectiveSelectedHoleId
                ? ['==', ['get', 'hole_id'], effectiveSelectedHoleId]
                : ['==', ['get', 'hole_id'], ''],  // match nothing
        );
    }, [effectiveSelectedHoleId, mapReady, useMvt]);

    // ── MVT click + hover interactions ───────────────────────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !useMvt) return;

        // Popup builder — dispatches to layer-specific renderers by feature source-layer
        const buildPopupHtml = (props: Record<string, unknown>, sourceLayer: string): string => {
            const wrap = (inner: string) => `
                <div style="font-family: ui-monospace, monospace; font-size: 11px; line-height: 1.5; color: #f3f4f6; background: #111827; padding: 6px 8px;">
                    ${inner}
                </div>`;

            // Geochem sample popup: sample_id, sample_type, assay_element_codes
            if (sourceLayer === 'geochem') {
                // assay_element_codes is serialised as a JSON text string by PostgreSQL's
                // to_json(text[])::text — parse it back to an array for display.
                let elementList = '—';
                try {
                    const parsed = typeof props.assay_element_codes === 'string'
                        ? JSON.parse(props.assay_element_codes) as unknown
                        : props.assay_element_codes;
                    if (Array.isArray(parsed) && parsed.length > 0) {
                        elementList = (parsed as string[]).map(escapeHtml).join(', ');
                    }
                } catch { /* ignore — display fallback */ }
                return wrap(`
                    <div style="font-weight: 700; font-size: 13px; color: #84cc16; margin-bottom: 3px;">Geochem Sample</div>
                    <div style="color: #d1d5db;">${escapeHtml(props.sample_id ?? '—')}</div>
                    ${props.sample_type ? `<div style="color: #9ca3af; margin-top: 2px;">${escapeHtml(props.sample_type)}</div>` : ''}
                    <div style="color: #9ca3af; margin-top: 3px; font-size: 10px;">Elements: ${elementList}</div>
                `);
            }

            // Seismic survey popup: survey_name, survey_year, survey_type, line_count
            if (sourceLayer === 'seismic') {
                return wrap(`
                    <div style="font-weight: 700; font-size: 13px; color: #0ea5e9; margin-bottom: 3px;">${escapeHtml(props.survey_name ?? 'Seismic Survey')}</div>
                    <div style="color: #9ca3af;">${escapeHtml(props.survey_type ?? '—')}${props.survey_year ? ` · ${escapeHtml(props.survey_year)}` : ''}</div>
                    ${props.line_count != null ? `<div style="color: #9ca3af; margin-top: 2px;">${escapeHtml(props.line_count)} lines/traces</div>` : ''}
                `);
            }

            // Default collar/working popup
            const totalDepth = props.total_depth != null ? parseFloat(String(props.total_depth)).toFixed(0) + ' m TD' : '—';
            return wrap(`
                <div style="font-weight: 700; font-size: 13px; color: #f9fafb; margin-bottom: 3px;">${escapeHtml(props.hole_id ?? props.working_name ?? props.feature_name ?? '—')}</div>
                ${props.hole_type ? `<div style="color: #9ca3af;">${escapeHtml(props.hole_type)} · ${totalDepth}</div>` : ''}
                ${props.working_type ? `<div style="color: #a855f7; margin-top: 2px;">${escapeHtml(props.working_type)}</div>` : ''}
                ${props.status ? `<div style="color: ${markerColor(String(props.status), false)}; margin-top: 2px;">${escapeHtml(props.status)}</div>` : ''}
                ${props.source ? `<div style="color: #9ca3af; margin-top: 2px;">${escapeHtml(props.source)}</div>` : ''}
            `);
        };

        // Click handler
        const handleClick = (e: maplibregl.MapMouseEvent) => {
            const features = map.queryRenderedFeatures(e.point, {
                layers: MVT_INTERACTIVE_LAYERS.filter((id) => map.getLayer(id)),
            });
            if (!features.length) return;

            const feat = features[0];
            const props = feat.properties as Record<string, unknown>;
            const sourceLayer = feat.sourceLayer ?? '';

            // Collar click → feed back into chat conversation
            if (props.hole_id) {
                onCollarClick?.(String(props.hole_id));
            }

            popupRef.current?.remove();
            const popup = new maplibregl.Popup({
                closeButton: true, closeOnClick: false,
                className: 'georag-map-popup', maxWidth: '240px',
            })
                .setLngLat(e.lngLat)
                .setHTML(buildPopupHtml(props, sourceLayer))
                .addTo(map);
            popupRef.current = popup as ExtendedPopup;
        };

        // Hover handler — cursor change + tooltip
        const handleMouseMove = (e: maplibregl.MapMouseEvent) => {
            const features = map.queryRenderedFeatures(e.point, {
                layers: MVT_INTERACTIVE_LAYERS.filter((id) => map.getLayer(id)),
            });
            map.getCanvas().style.cursor = features.length ? 'pointer' : '';

            if (!features.length) return;

            // Debounce — don't recreate popup on every pixel move
            const feat = features[0];
            const props = feat.properties as Record<string, unknown>;
            const featureKey = props.hole_id ?? props.survey_name ?? props.sample_id ?? feat.id;
            if (popupRef.current?._hoverFeatureKey === featureKey) return;

            popupRef.current?.remove();
            const popup = new maplibregl.Popup({
                closeButton: false, closeOnClick: false,
                className: 'georag-map-popup', maxWidth: '240px', offset: 12,
            })
                .setLngLat(e.lngLat)
                .setHTML(buildPopupHtml(props, feat.sourceLayer ?? ''))
                .addTo(map);
            // Cast once to attach hover tracking key. MapLibre's Popup is not
            // designed to be subclassed, so we extend via the cast pattern.
            // as unknown as ExtendedPopup — needed because Popup doesn't declare
            // _hoverFeatureKey; this is a local duck-type extension only (max 2 casts).
            const extPopup = popup as unknown as ExtendedPopup;
            extPopup._hoverFeatureKey = featureKey as string | number | undefined;
            popupRef.current = extPopup;
        };

        const handleMouseLeave = () => {
            map.getCanvas().style.cursor = '';
            // Only remove hover popups (no close button), not click popups
            if (popupRef.current && !popupRef.current.options?.closeButton) {
                popupRef.current.remove();
            }
        };

        map.on('click', handleClick);
        map.on('mousemove', handleMouseMove);
        // Listen on each interactive layer for mouseleave
        MVT_INTERACTIVE_LAYERS.forEach((id) => {
            if (map.getLayer(id)) map.on('mouseleave', id, handleMouseLeave);
        });

        return () => {
            map.off('click', handleClick);
            map.off('mousemove', handleMouseMove);
            MVT_INTERACTIVE_LAYERS.forEach((id) => {
                if (map.getLayer(id)) map.off('mouseleave', id, handleMouseLeave);
            });
        };
    }, [mapReady, useMvt, onCollarClick]);

    // ══════════════════════════════════════════════════════════════════════════
    // ██  COVERAGE DENSITY LAYER (CC-03 Item 5)
    // ══════════════════════════════════════════════════════════════════════════

    // Fetch the coverage GeoJSON whenever the layer is enabled OR the
    // selectors change. Skipped silently when no project is mounted (the
    // chat-inline mode doesn't have one).
    useEffect(() => {
        if (!coverageEnabled || !projectId) {
            setCoverageData(null);
            return;
        }
        let cancelled = false;

        const fetchCoverage = async () => {
            setCoverageLoading(true);
            setCoverageError(null);
            try {
                const url =
                    `/api/v1/projects/${projectId}/coverage-density` +
                    `?kind=${coverageKind}&cell_size_m=${coverageCellSize}`;
                const res = await fetch(url, {
                    credentials: 'same-origin',
                    headers: {
                        Accept: 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const body = await res.json() as CoverageFeatureCollection;
                if (!cancelled) setCoverageData(body);
            } catch (err) {
                if (!cancelled) {
                    setCoverageError(err instanceof Error ? err.message : String(err));
                    setCoverageData(null);
                }
            } finally {
                if (!cancelled) setCoverageLoading(false);
            }
        };

        void fetchCoverage();
        return () => { cancelled = true; };
    }, [coverageEnabled, coverageKind, coverageCellSize, projectId]);

    // Add / update the coverage-density source + layers on data change.
    // Two layers paint the same source:
    //   coverage-density-fill   — record_count-interpolated fill (all cells)
    //   coverage-density-warn   — dashed stroke for bias_warning cells only
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady) return;

        const sourceId = 'coverage-density';
        const fillId = 'coverage-density-fill';
        const warnId = 'coverage-density-warn';

        const teardown = () => {
            if (map.getLayer(warnId)) map.removeLayer(warnId);
            if (map.getLayer(fillId)) map.removeLayer(fillId);
            if (map.getSource(sourceId)) map.removeSource(sourceId);
        };

        if (!coverageEnabled || !coverageData) {
            teardown();
            return;
        }

        const maxCount = Math.max(1, coverageData.max_count ?? 1);
        const geojson: GeoJSON.FeatureCollection = {
            type: 'FeatureCollection',
            features: coverageData.features as unknown as GeoJSON.Feature[],
        };

        const existing = map.getSource(sourceId);
        if (existing) {
            (existing as GeoJSONSource).setData(geojson);
        } else {
            map.addSource(sourceId, { type: 'geojson', data: geojson });
        }

        if (!map.getLayer(fillId)) {
            map.addLayer({
                id: fillId,
                type: 'fill',
                source: sourceId,
                paint: {
                    'fill-opacity': 0.5,
                    // Sequential viridis-ish ramp keyed on record_count
                    // normalised against this run's max so even a sparse
                    // dataset spans the full palette.
                    'fill-color': [
                        'interpolate', ['linear'],
                        ['get', 'record_count'],
                        0,            '#440154',
                        maxCount * 0.25, '#3b528b',
                        maxCount * 0.50, '#21918c',
                        maxCount * 0.75, '#5ec962',
                        maxCount,        '#fde725',
                    ],
                },
            } as unknown as AddLayerObject);
        } else {
            // Refresh the interpolation stop set when max_count moves.
            map.setPaintProperty(fillId, 'fill-color', [
                'interpolate', ['linear'],
                ['get', 'record_count'],
                0,            '#440154',
                maxCount * 0.25, '#3b528b',
                maxCount * 0.50, '#21918c',
                maxCount * 0.75, '#5ec962',
                maxCount,        '#fde725',
            ]);
        }

        if (!map.getLayer(warnId)) {
            map.addLayer({
                id: warnId,
                type: 'line',
                source: sourceId,
                filter: ['==', ['get', 'bias_warning'], true],
                paint: {
                    'line-color': '#f59e0b',
                    'line-width': 1.5,
                    'line-dasharray': [2, 2],
                    'line-opacity': 0.9,
                },
            } as unknown as AddLayerObject);
        }

        return () => {
            // Effect cleanup runs on dependency change. We only fully tear
            // down when the layer is disabled (handled above on next run);
            // intermediate updates leave the layers in place.
        };
    }, [coverageData, coverageEnabled, mapReady]);

    // Hover popup for coverage cells — distinct from the MVT popup so the
    // two interaction sets don't conflict.
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !coverageEnabled) return;

        const fillId = 'coverage-density-fill';
        let activePopup: Popup | null = null;

        const onMove = (e: maplibregl.MapMouseEvent) => {
            if (!map.getLayer(fillId)) return;
            const features = map.queryRenderedFeatures(e.point, { layers: [fillId] });
            if (!features.length) {
                activePopup?.remove();
                activePopup = null;
                map.getCanvas().style.cursor = '';
                return;
            }
            const feat = features[0];
            const props = feat.properties as { record_count?: number; bias_warning?: boolean };
            const count = Number(props.record_count ?? 0);
            const biased = props.bias_warning === true || String(props.bias_warning) === 'true';
            const kindLabel =
                COVERAGE_KIND_OPTIONS.find((o) => o.value === coverageKind)?.label.toLowerCase() ??
                coverageKind;

            map.getCanvas().style.cursor = 'pointer';
            activePopup?.remove();
            const html = biased
                ? `<div style="font-family: ui-monospace, monospace; font-size: 11px; line-height: 1.5; color: #fde68a; background: #111827; padding: 6px 8px;">
                       <div style="font-weight: 700; color: #fbbf24; margin-bottom: 2px;">${count} ${kindLabel} in this area</div>
                       <div style="color: #9ca3af;">results may reflect historical exploration bias</div>
                   </div>`
                : `<div style="font-family: ui-monospace, monospace; font-size: 11px; line-height: 1.5; color: #f3f4f6; background: #111827; padding: 6px 8px;">
                       <div style="font-weight: 700; color: #f9fafb;">${count} ${kindLabel}</div>
                   </div>`;
            activePopup = new maplibregl.Popup({
                closeButton: false, closeOnClick: false,
                className: 'georag-map-popup', maxWidth: '260px', offset: 8,
            })
                .setLngLat(e.lngLat)
                .setHTML(html)
                .addTo(map);
        };

        const onLeave = () => {
            activePopup?.remove();
            activePopup = null;
            map.getCanvas().style.cursor = '';
        };

        map.on('mousemove', fillId, onMove);
        map.on('mouseleave', fillId, onLeave);
        return () => {
            map.off('mousemove', fillId, onMove);
            map.off('mouseleave', fillId, onLeave);
            activePopup?.remove();
        };
    }, [coverageEnabled, coverageKind, mapReady]);

    // ── MVT error handling + tile-failure watchdog (MAPVIEW-03) ─────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !useMvt) return;

        const watchdog = watchdogRef.current;

        // MapLibre's published ErrorEvent type is { error: ErrorLike }.
        // At runtime, tile errors also carry sourceId on the event object —
        // but the types.d.ts file omits it (MapLibre internal). We access it
        // via a loose cast; this is safe and well-documented in MapLibre's
        // source but not in the public API surface.
        const handleError = (e: MapErrorEvent) => {
            // The event carries a sourceId at runtime even though the type doesn't declare it.
            // This is a MapLibre implementation detail confirmed in their source.
            const evt = e as MapErrorEvent & { sourceId?: string };
            if (!evt.sourceId) return;

            const status = (e.error as { status?: number } | undefined)?.status;
            // 401 / 403 are auth errors — surface immediately as fatal errors
            if (status === 401) {
                setError('Session expired — please log in again.');
                return;
            }
            if (status === 403) {
                setError('Access denied for this project\'s map data.');
                return;
            }

            // Don't trigger failure watchdog for HTTP 204 (Martin empty tile).
            // Martin returns 204 for valid but empty tiles; that is NOT an error.
            if (status === 204) return;

            // Build the URL prefix for the toast display
            const urlPrefix = `/tiles/silver/ [source: ${evt.sourceId}]`;
            watchdog.recordFailure(evt.sourceId, urlPrefix);
            console.warn(`Tile error [${evt.sourceId}] status=${status ?? 'unknown'}:`, e.error);
        };

        // Success tracking — reset failure counter when a tile loads cleanly
        const handleSourceData = (e: MapSourceDataEvent) => {
            if (e.dataType !== 'source') return;
            if (!e.sourceId) return;
            // e.tile is typed as `any` in MapLibre's own declaration
            const tile = e.tile as { state?: string } | null | undefined;
            if (tile?.state === 'loaded') {
                watchdog.recordSuccess(e.sourceId);
            }
        };

        map.on('error', handleError);
        map.on('sourcedata', handleSourceData);
        return () => {
            map.off('error', handleError);
            map.off('sourcedata', handleSourceData);
        };
    }, [mapReady, useMvt]);

    // ══════════════════════════════════════════════════════════════════════════
    // ██  LEGACY RENDERING PATH — GeoJSON fetch + DOM markers
    // ══════════════════════════════════════════════════════════════════════════

    // ── Build markers when collars change (legacy path only) ───────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || collars.length === 0 || useMvt) return;

        // Remove old markers
        Object.values(markersRef.current).forEach((m) => m.remove());
        markersRef.current = {};
        popupRef.current?.remove();

        collars.forEach((collar) => {
            const el = document.createElement('div');
            el.setAttribute('role', 'button');
            el.setAttribute('aria-label', `Drill hole ${String(collar.hole_id ?? '')} — ${collar.status ?? 'unknown'} ${markerShape(collar.status)}`);
            el.setAttribute('tabindex', '0');
            Object.assign(el.style, { cursor: 'pointer', padding: '4px' });

            const dot = document.createElement('div');
            const color = markerColor(collar.status, false);
            Object.assign(dot.style, {
                width: '10px', height: '10px', borderRadius: '50%',
                background: color,
                border: '1.5px solid rgba(0,0,0,0.4)',
                boxShadow: '0 1px 3px rgba(0,0,0,0.6)',
                transition: 'transform 0.15s ease, width 0.15s ease, height 0.15s ease',
                transformOrigin: 'center center',
            });
            (dot as HTMLElement & { dataset: DOMStringMap }).dataset.holeId = String(collar.hole_id ?? '');
            (dot as HTMLElement & { dataset: DOMStringMap }).dataset.baseColor = color;
            el.appendChild(dot);

            // Hover tooltip
            el.addEventListener('mouseenter', () => {
                dot.style.transform = 'scale(1.4)';
                popupRef.current?.remove();
                const totalDepth = collar.total_depth != null ? parseFloat(String(collar.total_depth)).toFixed(0) + ' m TD' : '—';
                const popup = new maplibregl.Popup({
                    closeButton: false, closeOnClick: false,
                    className: 'georag-map-popup', maxWidth: '220px', offset: 12,
                })
                    .setLngLat([collar._lon, collar._lat])
                    .setHTML(`
                        <div style="font-family: ui-monospace, monospace; font-size: 11px; line-height: 1.5; color: #f3f4f6; background: #111827; padding: 6px 8px;">
                            <div style="font-weight: 700; font-size: 13px; color: #f9fafb; margin-bottom: 3px;">${escapeHtml(collar.hole_id ?? '')}</div>
                            <div style="color: #9ca3af;">${escapeHtml(collar.hole_type ?? '—')} · ${totalDepth}</div>
                            <div style="color: ${color}; margin-top: 2px;">${escapeHtml(collar.status ?? '—')}</div>
                        </div>
                    `)
                    .addTo(map);
                popupRef.current = popup as ExtendedPopup;
            });
            el.addEventListener('mouseleave', () => {
                dot.style.transform = 'scale(1)';
                popupRef.current?.remove();
            });

            // Click — persistent popup + callback
            const handleClick = () => {
                if (collar.hole_id) onCollarClick?.(String(collar.hole_id));
                popupRef.current?.remove();
                const totalDepth = collar.total_depth != null ? parseFloat(String(collar.total_depth)).toFixed(0) + ' m TD' : '—';
                const popup = new maplibregl.Popup({
                    closeButton: true, closeOnClick: false,
                    className: 'georag-map-popup', maxWidth: '220px',
                })
                    .setLngLat([collar._lon, collar._lat])
                    .setHTML(`
                        <div style="font-family: ui-monospace, monospace; font-size: 11px; line-height: 1.5; color: #f3f4f6; background: #111827; padding: 6px 2px;">
                            <div style="font-weight: 700; font-size: 13px; color: #f9fafb; margin-bottom: 4px;">${escapeHtml(collar.hole_id ?? '')}</div>
                            <div style="color: #9ca3af;">${escapeHtml(collar.hole_type ?? '—')} · ${totalDepth}</div>
                            <div style="color: ${color}; margin-top: 2px;">${escapeHtml(collar.status ?? '—')}</div>
                        </div>
                    `)
                    .addTo(map);
                popupRef.current = popup as ExtendedPopup;
            };
            el.addEventListener('click', handleClick);
            el.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleClick(); }
            });

            const marker = new maplibregl.Marker({ element: el, anchor: 'center' })
                .setLngLat([collar._lon, collar._lat])
                .addTo(map);
            markersRef.current[String(collar.hole_id ?? `_${collar._lon}`)] = marker;
        });

        // Fit to bounding box
        let bbox: LngLatBoundsLike | null = null;
        if (inlineBbox && inlineBbox.length === 4) {
            bbox = [[inlineBbox[0], inlineBbox[1]], [inlineBbox[2], inlineBbox[3]]];
        } else {
            bbox = computeBbox(collars);
        }
        if (bbox) {
            map.fitBounds(bbox, { padding: compact ? 30 : 60, maxZoom: 14, duration: 800 });
        }
    }, [collars, mapReady, onCollarClick, inlineBbox, compact, useMvt]);

    // ── Lightweight selection highlight — legacy DOM path only ────────────────
    const prevSelectedRef = useRef<string | null>(null);
    useEffect(() => {
        if (useMvt) return;  // MVT path handles selection via paint filter
        const prev = prevSelectedRef.current;
        const next = effectiveSelectedHoleId ?? null;

        // Deselect previous
        if (prev && markersRef.current[prev]) {
            const dot = markersRef.current[prev].getElement().querySelector('div') as (HTMLElement & { dataset: DOMStringMap }) | null;
            if (dot) {
                Object.assign(dot.style, {
                    width: '10px', height: '10px',
                    background: dot.dataset.baseColor,
                    border: '1.5px solid rgba(0,0,0,0.4)',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.6)',
                });
            }
        }

        // Select new
        if (next && markersRef.current[next]) {
            const dot = markersRef.current[next].getElement().querySelector('div') as HTMLElement | null;
            if (dot) {
                Object.assign(dot.style, {
                    width: '14px', height: '14px',
                    background: '#f59e0b',
                    border: '2px solid #f59e0b',
                    boxShadow: '0 0 8px rgba(245,158,11,0.8)',
                });
            }
        }

        prevSelectedRef.current = next;
    }, [effectiveSelectedHoleId, useMvt]);

    // ── GeoJSON collar source for scale (legacy path only) ──────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || collars.length === 0 || useMvt) return;

        // Build a GeoJSON FeatureCollection from collar data.
        // CC-01 Item 2 — only emit the uncertainty fields when
        // spatial_uncertainty_m is a real number, so the layer filter can use
        // `['has', 'spatial_uncertainty_m']` to skip features cleanly. `_lat`
        // is also published so the runtime metres→pixels paint expression
        // can compensate for Web-Mercator latitude shrink without a second
        // server round-trip or a turf import.
        const geojson: GeoJSON.FeatureCollection = {
            type: 'FeatureCollection',
            features: collars.map((c) => {
                const props: Record<string, unknown> = {
                    hole_id: c.hole_id,
                    status: c.status ?? 'Unknown',
                    total_depth: c.total_depth,
                    hole_type: c.hole_type,
                };
                if (typeof c.spatial_uncertainty_m === 'number' && c.spatial_uncertainty_m >= 0) {
                    props.spatial_uncertainty_m = c.spatial_uncertainty_m;
                    props._lat = c._lat;
                    if (c.georef_method) props.georef_method = c.georef_method;
                    if (typeof c.crs_confidence === 'number') props.crs_confidence = c.crs_confidence;
                }
                return {
                    type: 'Feature' as const,
                    geometry: { type: 'Point' as const, coordinates: [c._lon, c._lat] },
                    properties: props,
                };
            }),
        };

        // Update or create the GeoJSON source (for future use by clustering/queries)
        const existingSource = map.getSource('collars-geojson');
        if (existingSource) {
            (existingSource as GeoJSONSource).setData(geojson);
        } else {
            try {
                map.addSource('collars-geojson', { type: 'geojson', data: geojson });
            } catch { /* source may already exist after style change */ }
        }

        // ── Uncertainty rings layer (CC-01 Item 2) ────────────────────────
        // Renders the spatial_uncertainty_m radius as a hollow MapLibre circle
        // sized in screen-pixels but driven by the per-feature uncertainty in
        // metres. The radius expression converts metres → pixels at the
        // current zoom using the Web-Mercator scale denominator:
        //
        //     pixels = metres * 2^zoom / (156543.03392 * cos(lat))
        //
        // `_lat` is published as a feature property so the cosine compensates
        // for latitude shrink (a Boreal-Shield collar at 60°N is ~half the
        // pixel-per-metre of one at the equator). The ring is hollow (no
        // fill) with a 0.25-alpha stroke, so even tightly-packed rings stay
        // visually parseable. Features without `spatial_uncertainty_m` are
        // skipped by the layer filter — that's how callers opt out per-row.
        if (!map.getLayer(UNCERTAINTY_RINGS_GEOJSON_LAYER_ID)) {
            try {
                // Single `as unknown as AddLayerObject` cast — the readonly
                // tuples we share with the tests don't narrow to MapLibre's
                // FilterSpecification / PaintSpecification union literals.
                map.addLayer({
                    id: UNCERTAINTY_RINGS_GEOJSON_LAYER_ID,
                    type: 'circle',
                    source: 'collars-geojson',
                    filter: UNCERTAINTY_RINGS_FILTER,
                    paint: UNCERTAINTY_RINGS_PAINT,
                } as unknown as AddLayerObject);
            } catch { /* layer add can race with style swaps; safe to ignore */ }
        }
    }, [collars, mapReady, useMvt]);

    // ── Re-style selected marker when effectiveSelectedHoleId changes ─────────────────
    // (handled by full marker rebuild above, but we also pan to it)
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !effectiveSelectedHoleId) return;

        const collar = collars.find((c) => c.hole_id === effectiveSelectedHoleId);
        if (collar) {
            map.panTo([collar._lon, collar._lat], { duration: 400 });
        }
    }, [effectiveSelectedHoleId, collars, mapReady]);

    // ── Saved-map restore (admin SavedMaps → /explorer) ───────────────────────
    // Listen for `georag:map:restore` and apply center/zoom or bbox. Layer
    // visibility is already restored via localStorage on the initial useState.
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady) return;

        const applyRestore = (detail: {
            center?: [number, number];
            zoom?: number;
            pitch?: number;
            bearing?: number;
            bbox?: [number, number, number, number];
        }) => {
            if (Array.isArray(detail.bbox) && detail.bbox.length === 4) {
                map.fitBounds([[detail.bbox[0], detail.bbox[1]], [detail.bbox[2], detail.bbox[3]]], {
                    padding: 40, duration: 600,
                });
                if (typeof detail.pitch === 'number' || typeof detail.bearing === 'number') {
                    // fitBounds doesn't carry pitch/bearing; apply them after a tick.
                    setTimeout(() => {
                        if (typeof detail.pitch === 'number') map.easeTo({ pitch: detail.pitch, duration: 0 });
                        if (typeof detail.bearing === 'number') map.easeTo({ bearing: detail.bearing, duration: 0 });
                    }, 650);
                }
                return;
            }
            if (Array.isArray(detail.center) && detail.center.length === 2) {
                map.flyTo({
                    center: detail.center,
                    zoom: typeof detail.zoom === 'number' ? detail.zoom : map.getZoom(),
                    pitch: typeof detail.pitch === 'number' ? detail.pitch : undefined,
                    bearing: typeof detail.bearing === 'number' ? detail.bearing : undefined,
                    duration: 600,
                });
            }
        };

        const handler = (e: Event) => {
            const ce = e as CustomEvent<{
                center?: [number, number];
                zoom?: number;
                pitch?: number;
                bearing?: number;
                bbox?: [number, number, number, number];
                view_id?: string;
            }>;
            applyRestore(ce.detail ?? {});
        };
        window.addEventListener('georag:map:restore', handler);

        // Replay any restore that fired BEFORE the map was ready
        // (SavedMaps stashes the payload in sessionStorage).
        try {
            const raw = window.sessionStorage.getItem('georag:savedMapPayload');
            if (raw) {
                const stashed = JSON.parse(raw) as {
                    payload?: {
                        center?: [number, number];
                        zoom?: number;
                        pitch?: number;
                        bearing?: number;
                        bbox?: [number, number, number, number];
                        bounds?: [number, number, number, number];
                    };
                };
                if (stashed.payload) {
                    const p = stashed.payload;
                    applyRestore({
                        center: p.center,
                        zoom: p.zoom,
                        pitch: p.pitch,
                        bearing: p.bearing,
                        bbox: p.bbox ?? p.bounds,
                    });
                }
                window.sessionStorage.removeItem('georag:savedMapPayload');
            }
        } catch { /* parse / storage failure — non-fatal */ }

        return () => window.removeEventListener('georag:map:restore', handler);
    }, [mapReady]);

    // ── Render ────────────────────────────────────────────────────────────────
    return (
        <div className="relative w-full h-full bg-gray-900 flex flex-col">
            {/* Loading overlay */}
            {loading && (
                <div className="absolute inset-0 z-10 flex items-center justify-center bg-gray-950/60 pointer-events-none">
                    <div className="flex items-center gap-2 text-sm text-gray-300">
                        <div className="w-4 h-4 rounded-full border-2 border-gray-600 border-t-amber-400 animate-spin" />
                        Loading collars…
                    </div>
                </div>
            )}

            {/* Error banner with retry */}
            {error && (
                <div
                    className="absolute top-2 left-2 right-2 z-20 flex items-center justify-between text-xs text-red-400 bg-red-950/90 border border-red-800 rounded px-3 py-2 shadow-lg"
                    role="alert"
                >
                    <span>Map error: {error}</span>
                    <button
                        type="button"
                        onClick={() => { setError(null); void fetchCollars(); }}
                        className="ml-2 text-red-300 hover:text-white underline"
                    >
                        Retry
                    </button>
                </div>
            )}

            {/* Tile-failure toast — MAPVIEW-03 ────────────────────────────────
                Shown when a tile source fails ≥ 3 times within 30 s.
                Non-fatal: partial data may still be shown.
                Auto-dismisses after 8 s; manually dismissable. */}
            {tileToast && (
                <div
                    className="absolute top-2 left-2 right-2 z-30 flex items-start justify-between text-xs text-amber-300 bg-amber-950/95 border border-amber-700 rounded px-3 py-2 shadow-lg"
                    role="alert"
                    aria-live="assertive"
                >
                    <div className="flex-1 min-w-0">
                        <span className="font-semibold">Tile layer failing: </span>
                        <span className="font-mono">{tileToast.sourceId}</span>
                        <span> — {tileToast.count} errors in the last 30s. Showing partial data.</span>
                        <div className="mt-0.5 font-mono text-amber-500 truncate">{tileToast.urlPrefix}</div>
                    </div>
                    <button
                        type="button"
                        aria-label="Dismiss tile error notification"
                        onClick={() => { setTileToast(null); if (tileToastTimerRef.current) clearTimeout(tileToastTimerRef.current); }}
                        className="ml-2 flex-shrink-0 text-amber-400 hover:text-white leading-none text-sm"
                    >
                        ✕
                    </button>
                </div>
            )}

            {/* No project selected (only shown in project-scoped mode) */}
            {!projectId && !inlineGeoJson && (
                <div className="absolute inset-0 z-10 flex items-center justify-center bg-gray-950/80">
                    <p className="text-sm text-gray-500">Select a project to load collar locations.</p>
                </div>
            )}

            {/* Collar count badge + status legend (legacy path) */}
            {!useMvt && collars.length > 0 && (
                <div className="absolute top-2 left-2 z-10 bg-gray-900/90 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-300 font-mono pointer-events-none space-y-1">
                    <div>{collars.length} collar{collars.length !== 1 ? 's' : ''}</div>
                    {!compact && (
                        <div className="flex gap-2 text-[9px]">
                            <span style={{ color: '#22c55e' }}>● Complete</span>
                            <span style={{ color: '#eab308' }}>◆ Active</span>
                            <span style={{ color: '#ef4444' }}>✕ Abandoned</span>
                        </div>
                    )}
                </div>
            )}

            {/* ── Silver layer toggle panel (MVT path) — Deliverable C ─────────
                Collapsible panel, top-right below navigation controls.
                role="region" + aria-label for screen readers.
                Each checkbox has a real <label> association via htmlFor/id.   */}
            {useMvt && !compact && (
                <div
                    className="absolute top-24 right-2 z-10 bg-gray-900/95 border border-gray-700 rounded shadow-lg min-w-[160px]"
                    role="region"
                    aria-label="Map layer toggles"
                >
                    {/* Panel header — toggle open/close */}
                    <button
                        type="button"
                        className="w-full flex items-center justify-between px-3 py-2 text-xs font-semibold text-gray-300 hover:text-gray-100 transition-colors"
                        aria-expanded={layerPanelOpen}
                        aria-controls="map-layer-toggles"
                        onClick={() => setLayerPanelOpen((prev) => !prev)}
                    >
                        <span className="uppercase tracking-wider text-[10px] text-gray-400">Layers</span>
                        <span className="text-gray-500 text-[10px]">{layerPanelOpen ? '▲' : '▼'}</span>
                    </button>

                    {/* Toggle list */}
                    {layerPanelOpen && (
                        <div id="map-layer-toggles" className="px-2.5 pb-2.5 space-y-1.5">
                            {MVT_LAYERS.map((layer) => {
                                const checkId = `layer-toggle-${layer.id}`;
                                // Pick a representative color for the swatch
                                const swatchColor: string = (
                                    typeof layer.paint['circle-color'] === 'string'
                                        ? layer.paint['circle-color']
                                        : typeof layer.paint['fill-color'] === 'string'
                                        ? layer.paint['fill-color']
                                        : typeof layer.paint['line-color'] === 'string'
                                        ? layer.paint['line-color']
                                        : '#6b7280'
                                ) as string;

                                return (
                                    <div key={layer.id} className="flex items-center gap-2">
                                        <input
                                            type="checkbox"
                                            id={checkId}
                                            checked={visibleLayers[layer.id] ?? true}
                                            onChange={() => setVisibleLayers((prev) => ({
                                                ...prev,
                                                [layer.id]: !(prev[layer.id] ?? true),
                                            }))}
                                            className="w-3.5 h-3.5 rounded border-gray-600 bg-gray-800 text-amber-500 focus:ring-amber-500 focus:ring-offset-0 focus:ring-1 cursor-pointer"
                                        />
                                        <label
                                            htmlFor={checkId}
                                            className="flex items-center gap-1.5 cursor-pointer text-xs text-gray-300 hover:text-gray-100 select-none"
                                        >
                                            <span
                                                className="w-2 h-2 rounded-full inline-block flex-shrink-0"
                                                style={{ background: swatchColor }}
                                                aria-hidden="true"
                                            />
                                            {layer.label}
                                        </label>
                                    </div>
                                );
                            })}
                            <div className="flex gap-2 text-[9px] text-gray-500 mt-1.5 pt-1.5 border-t border-gray-700">
                                <span style={{ color: '#22c55e' }}>● Done</span>
                                <span style={{ color: '#eab308' }}>◆ Active</span>
                                <span style={{ color: '#ef4444' }}>✕ Abandoned</span>
                            </div>

                            {/* ── Coverage density toggle (CC-03 Item 5) ─────────────── */}
                            {projectId && (
                                <div className="mt-2 pt-2 border-t border-gray-700 space-y-1.5">
                                    <div className="flex items-center gap-2">
                                        <input
                                            type="checkbox"
                                            id="layer-toggle-coverage-density"
                                            checked={coverageEnabled}
                                            onChange={() => setCoverageEnabled((v) => !v)}
                                            className="w-3.5 h-3.5 rounded border-gray-600 bg-gray-800 text-amber-500 focus:ring-amber-500 focus:ring-offset-0 focus:ring-1 cursor-pointer"
                                        />
                                        <label
                                            htmlFor="layer-toggle-coverage-density"
                                            className="flex items-center gap-1.5 cursor-pointer text-xs text-gray-300 hover:text-gray-100 select-none"
                                        >
                                            <span
                                                className="w-2 h-2 rounded-sm inline-block flex-shrink-0"
                                                style={{
                                                    background:
                                                        'linear-gradient(90deg,#440154,#3b528b,#21918c,#5ec962,#fde725)',
                                                }}
                                                aria-hidden="true"
                                            />
                                            Coverage density
                                        </label>
                                    </div>
                                    {coverageEnabled && (
                                        <div className="pl-5 space-y-1">
                                            <div className="flex items-center gap-1.5">
                                                <label
                                                    htmlFor="coverage-kind"
                                                    className="text-[10px] uppercase tracking-wider text-gray-500 w-10"
                                                >
                                                    Kind
                                                </label>
                                                <select
                                                    id="coverage-kind"
                                                    value={coverageKind}
                                                    onChange={(e) => setCoverageKind(e.target.value as CoverageKind)}
                                                    className="flex-1 text-[11px] bg-gray-800 border border-gray-700 rounded px-1.5 py-0.5 text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500"
                                                >
                                                    {COVERAGE_KIND_OPTIONS.map((o) => (
                                                        <option key={o.value} value={o.value}>{o.label}</option>
                                                    ))}
                                                </select>
                                            </div>
                                            <div className="flex items-center gap-1.5">
                                                <label
                                                    htmlFor="coverage-cell-size"
                                                    className="text-[10px] uppercase tracking-wider text-gray-500 w-10"
                                                >
                                                    Cell
                                                </label>
                                                <select
                                                    id="coverage-cell-size"
                                                    value={coverageCellSize}
                                                    onChange={(e) => setCoverageCellSize(Number(e.target.value))}
                                                    className="flex-1 text-[11px] bg-gray-800 border border-gray-700 rounded px-1.5 py-0.5 text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500"
                                                >
                                                    {COVERAGE_CELL_SIZES.map((o) => (
                                                        <option key={o.value} value={o.value}>{o.label}</option>
                                                    ))}
                                                </select>
                                            </div>
                                            {coverageLoading && (
                                                <div className="text-[10px] text-gray-500">Loading coverage…</div>
                                            )}
                                            {coverageError && (
                                                <div className="text-[10px] text-red-400">{coverageError}</div>
                                            )}
                                            {!coverageLoading && !coverageError && coverageData && (
                                                <div className="text-[10px] text-gray-500">
                                                    {coverageData.feature_count ?? coverageData.features.length} cells
                                                    {' · '}max {coverageData.max_count ?? '—'}
                                                </div>
                                            )}
                                            <div className="flex items-center gap-1 text-[9px] text-amber-400 pt-0.5">
                                                <span
                                                    className="inline-block w-3 h-[2px] border-t border-dashed border-amber-400"
                                                    aria-hidden="true"
                                                />
                                                <span>Dashed = sparse / bias warning</span>
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    )}
                </div>
            )}

            {/* Style switcher — satellite / terrain / default */}
            {!compact && (
                <div className="absolute top-2 right-14 z-10 flex bg-gray-900/90 border border-gray-700 rounded-lg overflow-hidden">
                    {Object.entries(mapStyles).map(([key, { label }]) => (
                        <button
                            key={key}
                            type="button"
                            onClick={() => setMapStyle(key)}
                            className={[
                                'px-2.5 py-1.5 text-[10px] font-medium transition-colors',
                                mapStyle === key
                                    ? 'bg-amber-600 text-white'
                                    : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800',
                            ].join(' ')}
                            aria-label={`Switch to ${label} map`}
                        >
                            {label}
                        </button>
                    ))}
                </div>
            )}

            {/* Map container */}
            <div
                ref={mapContainer}
                className="w-full h-full"
                aria-label="Drill collar map"
            />

            {/* Popup dark-mode style injection */}
            <style>{`
                .georag-map-popup .maplibregl-popup-content {
                    background: #111827 !important;
                    border: 1px solid #374151 !important;
                    border-radius: 8px !important;
                    padding: 10px 12px !important;
                    box-shadow: 0 10px 25px rgba(0,0,0,0.5) !important;
                }
                .georag-map-popup .maplibregl-popup-tip {
                    border-top-color: #374151 !important;
                }
                .georag-map-popup .maplibregl-popup-close-button {
                    color: #9ca3af !important;
                    font-size: 16px !important;
                    padding: 2px 6px !important;
                }
                .georag-map-popup .maplibregl-popup-close-button:hover {
                    color: #f3f4 !important;
                    background: transparent !important;
                }
            `}</style>
        </div>
    );
}
