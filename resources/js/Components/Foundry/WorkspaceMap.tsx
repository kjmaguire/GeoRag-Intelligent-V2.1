import { useEffect, useRef, useState } from 'react';
import { router } from '@inertiajs/react';
import { useBasemapStyleUrl } from '@/lib/basemap';

// CC-01 Item 2 — closed vocabulary for the georef_method column. Mirrors the
// chk_*_georef_method DB constraint. Kept in sync with the same type in
// resources/js/Components/MapView.tsx.
export type GeorefMethod = 'declared' | 'detected' | 'assumed' | 'manual' | 'survey';

export interface MapCollar {
    collar_id: string;
    hole_id: string;
    hole_id_canonical: string;
    total_depth: number | null;
    lat: number | null;
    lng: number | null;
    ore_bands: number;
    ore_thickness_m: number;
    // CC-01 Item 2 — spatial uncertainty triple. Optional / nullable; the
    // uncertainty-rings layer filter (`['has', 'spatial_uncertainty_m']`)
    // skips features whose source row didn't publish the field.
    spatial_uncertainty_m?: number | null;
    crs_confidence?: number | null;
    georef_method?: GeorefMethod | null;
}

export interface MapProjectInfo {
    project_name: string;
    company: string | null;
    commodity: string | null;
    region: string | null;
    crs_epsg: number | null;
}

export interface MapProjectSummary {
    total_drilled_m: number;
    mean_td_m: number | null;
    ore_hole_count: number;
    total_ore_thickness_m: number;
    mean_u3o8_pct: number | null;
}

/**
 * Workspace MapLibre canvas.
 *
 * - dark_matter basemap from useBasemapStyleUrl
 * - GeoJSON collar layer with brighter halo on ore-bearing holes
 * - Heatmap layer weighted by ore_thickness_m (toggle-driven)
 * - Click → in-map detail panel with "View in LOGS" jump
 * - Hover → floating tooltip with hole id + ore stats
 * - Layer toggles drive marker visibility + heatmap visibility
 * - Project header overlay (name, operator, commodity, region)
 * - Stats chip (collars, drilled m, ore holes, ore thickness, mean grade)
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type GeoJsonGeometry = any;

export type BasemapId = 'dark_matter' | 'positron' | 'bright' | 'satellite';
export type MapTool = 'pan' | 'draw' | 'measure' | 'select';

// Static style URLs for the basemaps that aren't in the central
// useBasemapStyleUrl registry. Satellite uses Esri World Imagery (free,
// no key) wrapped in a minimal MapLibre style we build inline.
const BASEMAP_URLS: Record<BasemapId, string | object> = {
    dark_matter: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
    positron: 'https://tiles.openfreemap.org/styles/positron',
    bright: 'https://tiles.openfreemap.org/styles/bright',
    satellite: {
        version: 8,
        sources: {
            'esri-imagery': {
                type: 'raster',
                tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
                tileSize: 256,
                maxzoom: 19,
                attribution: 'Tiles © Esri',
            },
        },
        layers: [
            { id: 'esri-imagery', type: 'raster', source: 'esri-imagery' },
        ],
        glyphs: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/glyphs/{fontstack}/{range}.pbf',
    },
};

export function WorkspaceMap({
    collars,
    projectSlug,
    projectInfo,
    projectSummary,
    visibleLayers,
    projectAoi,
    onJumpToLogs,
    activeHole,
    setActiveHole,
    compareSet,
    onToggleCompare,
    onOpenCompare,
    onClearCompare,
    basemap,
    onBasemapChange,
    terrainOn,
    onTerrainChange,
    activeTool,
    onToolChange,
    height = '100%',
}: {
    collars: MapCollar[];
    projectSlug: string;
    projectInfo: MapProjectInfo;
    projectSummary: MapProjectSummary;
    visibleLayers: Record<string, boolean>;
    projectAoi: GeoJsonGeometry | null;
    onJumpToLogs?: (holeId: string) => void;
    activeHole: MapCollar | null;
    setActiveHole: (h: MapCollar | null) => void;
    compareSet: string[];
    onToggleCompare: (holeId: string) => void;
    onOpenCompare: () => void;
    onClearCompare: () => void;
    basemap: BasemapId;
    onBasemapChange: (b: BasemapId) => void;
    terrainOn: boolean;
    onTerrainChange: (on: boolean) => void;
    activeTool: MapTool;
    onToolChange: (t: MapTool) => void;
    height?: number | string;
}) {
    const containerRef = useRef<HTMLDivElement>(null);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const mapRef = useRef<any>(null);
    // Registry-driven default (dark_matter) plus per-basemap overrides.
    const registryStyle = useBasemapStyleUrl('dark_matter');
    const styleSpec = basemap === 'dark_matter' ? registryStyle : BASEMAP_URLS[basemap];
    const [hoverHole, setHoverHole] = useState<{ hole: MapCollar; x: number; y: number } | null>(null);
    // Drag-box state for the Select tool (screen-space pixel rect).
    const [selectRect, setSelectRect] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null);
    const [selectedHoles, setSelectedHoles] = useState<string[]>([]);
    const activeToolRef = useRef<MapTool>(activeTool);
    useEffect(() => {
        activeToolRef.current = activeTool;
    }, [activeTool]);
    // Keep the latest setActiveHole in a ref so the map's persistent click
    // handler always uses the current setter (avoids stale closure when
    // the parent re-renders).
    const setActiveHoleRef = useRef(setActiveHole);
    useEffect(() => {
        setActiveHoleRef.current = setActiveHole;
    }, [setActiveHole]);
    // Mirror compareSet into a ref so the map's click handler (defined inside
    // a useEffect that doesn't depend on compareSet to avoid tearing down the
    // map on every queue change) sees the current value.
    const compareSetRef = useRef<string[]>(compareSet);
    useEffect(() => {
        compareSetRef.current = compareSet;
    }, [compareSet]);

    useEffect(() => {
        if (!containerRef.current) return;
        let cancelled = false;

        const points = collars
            .filter((c) => c.lat !== null && c.lng !== null)
            .map((c) => {
                // CC-01 Item 2 — only attach uncertainty props when present.
                // The uncertainty-rings layer filter is `['has',
                // 'spatial_uncertainty_m']`; omitting the key (rather than
                // emitting null) is what skips features whose source row
                // didn't carry the value.
                const properties: Record<string, unknown> = {
                    collar_id: c.collar_id,
                    hole_id: c.hole_id_canonical,
                    total_depth: c.total_depth,
                    ore_bands: c.ore_bands,
                    ore_thickness_m: c.ore_thickness_m,
                };
                if (c.spatial_uncertainty_m != null) {
                    properties.spatial_uncertainty_m = c.spatial_uncertainty_m;
                    // _lat is consumed by the cosine-of-latitude correction in
                    // the circle-radius expression — must come along for the ride.
                    properties._lat = c.lat;
                }
                if (c.crs_confidence != null) {
                    properties.crs_confidence = c.crs_confidence;
                }
                if (c.georef_method != null) {
                    properties.georef_method = c.georef_method;
                }
                return {
                    type: 'Feature' as const,
                    geometry: { type: 'Point' as const, coordinates: [c.lng as number, c.lat as number] },
                    properties,
                };
            });

        if (points.length === 0) return;

        const lngs = points.map((p) => p.geometry.coordinates[0]);
        const lats = points.map((p) => p.geometry.coordinates[1]);
        const bounds: [number, number, number, number] = [
            Math.min(...lngs) - 0.01,
            Math.min(...lats) - 0.01,
            Math.max(...lngs) + 0.01,
            Math.max(...lats) + 0.01,
        ];

        import('maplibre-gl').then((ml) => {
            if (cancelled || !containerRef.current) return;
            const maplibregl = ml.default ?? ml;

            if (mapRef.current?.remove) {
                mapRef.current.remove();
            }

            const map = new maplibregl.Map({
                container: containerRef.current,
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                style: styleSpec as any,
                bounds,
                fitBoundsOptions: { padding: 60, maxZoom: 15 },
                attributionControl: false,
                // Allow zooming much closer than the default 22 maxZoom; the
                // halo + label interpolation stops at 22 so going past that
                // just keeps geometry crisp.
                maxZoom: 22,
            });

            map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
            map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left');

            map.on('load', () => {
                if (cancelled) return;

                // Terrain DEM source — AWS Open Terrain Tiles (USGS 3DEP /
                // NASA SRTM, public-domain underlying data). Source is
                // always added; setTerrain is toggled by the effect below
                // so users can flip 3D shading on/off without re-styling.
                try {
                    map.addSource('terrain-dem', {
                        type: 'raster-dem',
                        tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
                        tileSize: 256,
                        maxzoom: 15,
                        encoding: 'terrarium',
                    });
                } catch (e) {
                    // eslint-disable-next-line no-console
                    console.warn('[workspace-map] terrain source add failed', e);
                }

                map.addSource('collars', {
                    type: 'geojson',
                    data: { type: 'FeatureCollection', features: points },
                    // Cluster nearby collars so densely-drilled sections don't
                    // render as a 100-dot pile-up at low zoom. Clusters break
                    // apart automatically as you zoom in. Removed
                    // clusterProperties — it was a silent failure point and
                    // the brighter accent for ore-bearing clusters isn't
                    // worth the risk of disabling clustering entirely.
                    cluster: true,
                    // clusterMaxZoom must be strictly less than the source's
                    // implicit maxzoom (default 18 for clustered GeoJSON
                    // sources). Setting it to 18 triggers a console warning
                    // and the source falls back to defaults, leaving
                    // getClusterLeaves in a half-initialised state.
                    clusterMaxZoom: 17,
                    clusterRadius: 50,
                });

                // Synthetic drillhole-trace lines (one per collar). The trace
                // points "south" from the collar by a fixed angular offset
                // scaled to total_depth so deeper holes have longer ticks.
                // ~0.0001 deg lat ≈ 11 m, so 500 m TD ≈ a 0.0045-deg tick.
                const traceFeatures = points
                    .filter((p) => p.properties.total_depth !== null)
                    .map((p) => {
                        const td = Number(p.properties.total_depth ?? 0);
                        const lng = p.geometry.coordinates[0];
                        const lat = p.geometry.coordinates[1];
                        const lenDeg = Math.min(0.008, Math.max(0.0008, td * 0.000015));

                        return {
                            type: 'Feature' as const,
                            geometry: {
                                type: 'LineString' as const,
                                coordinates: [[lng, lat], [lng, lat - lenDeg]],
                            },
                            properties: p.properties,
                        };
                    });
                map.addSource('collar-traces', {
                    type: 'geojson',
                    data: { type: 'FeatureCollection', features: traceFeatures },
                });
                map.addLayer({
                    id: 'collar-traces-line',
                    type: 'line',
                    source: 'collar-traces',
                    paint: {
                        'line-color': [
                            'case',
                            ['>', ['get', 'ore_bands'], 0], '#7dd97c',
                            '#5ca4ce',
                        ],
                        'line-width': [
                            'interpolate', ['linear'], ['zoom'],
                            8, 1,
                            14, 2.4,
                            18, 3.5,
                            22, 5,
                        ],
                        'line-opacity': 0.9,
                    },
                    layout: { visibility: 'none' },
                });

                // Project AOI polygon (convex hull of collars).
                if (projectAoi) {
                    map.addSource('project-aoi', {
                        type: 'geojson',
                        data: { type: 'Feature', geometry: projectAoi, properties: {} },
                    });
                    map.addLayer({
                        id: 'project-aoi-fill',
                        type: 'fill',
                        source: 'project-aoi',
                        paint: {
                            'fill-color': '#e8a36b',
                            'fill-opacity': 0.08,
                        },
                        layout: { visibility: 'none' },
                    });
                    map.addLayer({
                        id: 'project-aoi-line',
                        type: 'line',
                        source: 'project-aoi',
                        paint: {
                            'line-color': '#e8a36b',
                            'line-width': 2,
                            'line-dasharray': [3, 2],
                            'line-opacity': 0.9,
                        },
                        layout: { visibility: 'none' },
                    });
                }

                // Heatmap weighted by ore_thickness_m. No maxzoom cap — the
                // user wants it to stay visible when zooming in close.
                map.addLayer({
                    id: 'collars-heatmap',
                    type: 'heatmap',
                    source: 'collars',
                    paint: {
                        'heatmap-weight': [
                            'interpolate', ['linear'], ['get', 'ore_thickness_m'],
                            0, 0,
                            5, 0.5,
                            20, 0.85,
                            60, 1,
                        ],
                        'heatmap-intensity': [
                            'interpolate', ['linear'], ['zoom'],
                            8, 0.9,
                            14, 1.8,
                            18, 2.4,
                            22, 3,
                        ],
                        'heatmap-color': [
                            'interpolate', ['linear'], ['heatmap-density'],
                            0, 'rgba(0,0,0,0)',
                            0.2, 'rgba(50, 130, 70, 0.45)',
                            0.5, 'rgba(140, 200, 90, 0.7)',
                            0.8, 'rgba(230, 210, 70, 0.85)',
                            1, 'rgba(255, 240, 120, 0.95)',
                        ],
                        'heatmap-radius': [
                            'interpolate', ['linear'], ['zoom'],
                            8, 14,
                            14, 40,
                            18, 80,
                            22, 140,
                        ],
                        // Fade the heatmap out as the user zooms past the
                        // collars so the dots/halos can take over.
                        'heatmap-opacity': [
                            'interpolate', ['linear'], ['zoom'],
                            8, 1,
                            16, 0.85,
                            18, 0.55,
                            22, 0.3,
                        ],
                    },
                    layout: { visibility: 'none' },
                });

                map.addLayer({
                    id: 'collars-halo',
                    type: 'circle',
                    source: 'collars',
                    filter: ['all', ['!', ['has', 'point_count']], ['>', ['get', 'ore_bands'], 0]],
                    paint: {
                        'circle-radius': [
                            'interpolate', ['linear'], ['zoom'],
                            8, 9,
                            14, 18,
                            18, 28,
                            22, 40,
                        ],
                        'circle-color': '#7dd97c',
                        'circle-opacity': 0.28,
                        'circle-stroke-color': '#7dd97c',
                        'circle-stroke-width': 1.5,
                        'circle-stroke-opacity': 0.9,
                    },
                });

                map.addLayer({
                    id: 'collars-dot',
                    type: 'circle',
                    source: 'collars',
                    filter: ['!', ['has', 'point_count']],
                    paint: {
                        'circle-radius': [
                            'interpolate', ['linear'], ['zoom'],
                            8, 5,
                            14, 9,
                            18, 14,
                            22, 20,
                        ],
                        'circle-color': [
                            'case',
                            ['>', ['get', 'ore_bands'], 0], '#8fe28b',
                            '#7accee',
                        ],
                        'circle-stroke-color': '#0a0e14',
                        'circle-stroke-width': 1.5,
                        'circle-opacity': 1,
                    },
                });

                // Cluster bubble — surfaces total count.
                map.addLayer({
                    id: 'cluster-circles',
                    type: 'circle',
                    source: 'collars',
                    filter: ['has', 'point_count'],
                    paint: {
                        'circle-color': '#a8e6a3',
                        'circle-stroke-color': '#5fc25a',
                        'circle-stroke-width': 2.5,
                        'circle-radius': [
                            'step',
                            ['get', 'point_count'],
                            18,  // 0..4 → 18
                            5, 22,
                            10, 26,
                            25, 32,
                            50, 38,
                        ],
                        'circle-opacity': 0.92,
                    },
                });
                map.addLayer({
                    id: 'cluster-count',
                    type: 'symbol',
                    source: 'collars',
                    filter: ['has', 'point_count'],
                    layout: {
                        'text-field': ['concat', ['get', 'point_count_abbreviated'], ''],
                        'text-size': 13,
                        'text-font': ['Open Sans Bold', 'Arial Unicode MS Regular'],
                        'text-allow-overlap': true,
                    },
                    paint: {
                        'text-color': '#0a0e14',
                    },
                });

                // Compare-queue ring — drawn on top of the regular dot so
                // queued holes have a distinct amber outline. Filter is
                // updated by the toggle effect when compareSet changes.
                map.addLayer({
                    id: 'collars-compare-ring',
                    type: 'circle',
                    source: 'collars',
                    filter: ['all', ['!', ['has', 'point_count']], ['in', ['get', 'hole_id'], ['literal', []]]],
                    paint: {
                        'circle-radius': [
                            'interpolate', ['linear'], ['zoom'],
                            8, 9,
                            14, 14,
                            18, 20,
                            22, 28,
                        ],
                        'circle-color': 'rgba(0,0,0,0)',
                        'circle-stroke-color': '#e8a36b',
                        'circle-stroke-width': 2.5,
                        'circle-stroke-opacity': 1,
                    },
                });

                // Permanent labels — only appear once the user zooms in
                // enough that they don't crowd each other.
                map.addLayer({
                    id: 'collars-label',
                    type: 'symbol',
                    source: 'collars',
                    filter: ['!', ['has', 'point_count']],
                    minzoom: 13,
                    layout: {
                        'text-field': ['get', 'hole_id'],
                        'text-size': [
                            'interpolate', ['linear'], ['zoom'],
                            13, 10,
                            18, 13,
                            22, 16,
                        ],
                        'text-offset': [0, 1.2],
                        'text-anchor': 'top',
                        'text-allow-overlap': false,
                        'text-font': ['Open Sans Regular', 'Arial Unicode MS Regular'],
                    },
                    paint: {
                        'text-color': '#e8edf3',
                        'text-halo-color': '#0a0e14',
                        'text-halo-width': 1.5,
                        'text-halo-blur': 0.3,
                    },
                });

                // CC-01 Item 2 — uncertainty-rings layer ported from
                // MapView.tsx so the default Foundry/Workspace view also
                // surfaces spatial confidence. Same filter + paint shape
                // (kept in lockstep manually — there's no shared registry
                // because the two map components carry independent layer
                // stacks). Features without spatial_uncertainty_m are
                // skipped server-side by the GeoJSON builder above.
                //
                // circle-radius converts metres → screen pixels using the
                // standard Web-Mercator formula:
                //     pixels = metres * 2^zoom / (156543.03392 * cos(lat))
                // _lat is attached by the GeoJSON builder when the feature
                // has spatial_uncertainty_m set.
                map.addLayer({
                    id: 'uncertainty-rings',
                    type: 'circle',
                    source: 'collars',
                    filter: ['all',
                        ['!', ['has', 'point_count']],
                        ['has', 'spatial_uncertainty_m'],
                    ],
                    paint: {
                        'circle-color': 'rgba(0,0,0,0)',
                        'circle-stroke-width': 1.5,
                        'circle-opacity': 0.25,
                        'circle-stroke-opacity': 0.55,
                        'circle-radius': [
                            '*',
                            ['get', 'spatial_uncertainty_m'],
                            ['/',
                                ['^', 2, ['zoom']],
                                ['*', 156543.03392, ['cos', ['*', ['get', '_lat'], 0.017453292519943295]]],
                            ],
                        ],
                        'circle-stroke-color': [
                            'match',
                            ['get', 'georef_method'],
                            'declared', '#22c55e',
                            'detected', '#3b82f6',
                            'assumed',  '#f97316',
                            'manual',   '#a855f7',
                            'survey',   '#000000',
                            '#9ca3af',
                        ],
                    },
                });

                map.on('mouseenter', 'collars-dot', () => {
                    map.getCanvas().style.cursor = 'pointer';
                });
                map.on('mouseleave', 'collars-dot', () => {
                    map.getCanvas().style.cursor = '';
                    setHoverHole(null);
                });
                map.on('mousemove', 'collars-dot', (e: {
                    features?: { properties: Record<string, unknown> }[];
                    point: { x: number; y: number };
                }) => {
                    const f = e.features?.[0];
                    if (!f) return;
                    const p = f.properties;
                    setHoverHole({
                        hole: {
                            collar_id: String(p.collar_id),
                            hole_id: String(p.hole_id),
                            hole_id_canonical: String(p.hole_id),
                            total_depth: p.total_depth === null || p.total_depth === undefined ? null : Number(p.total_depth),
                            lat: null,
                            lng: null,
                            ore_bands: Number(p.ore_bands ?? 0),
                            ore_thickness_m: Number(p.ore_thickness_m ?? 0),
                        },
                        x: e.point.x,
                        y: e.point.y,
                    });
                });
                map.on('click', 'collars-dot', (e: { features?: { properties: Record<string, unknown> }[] }) => {
                    const f = e.features?.[0];
                    if (!f) return;
                    const p = f.properties;
                    const clickedHoleId = String(p.hole_id);

                    // If exactly one hole is already queued AND the user
                    // clicked a DIFFERENT hole, auto-add the new one and
                    // open the comparison — no need to check the checkbox
                    // a second time. Close any open popup so the modal can
                    // take the stage.
                    const queued = compareSetRef.current;
                    if (queued.length === 1 && queued[0] !== clickedHoleId) {
                        onToggleCompare(clickedHoleId);
                        setActiveHoleRef.current(null);
                        return;
                    }
                    setActiveHoleRef.current({
                        collar_id: String(p.collar_id),
                        hole_id: clickedHoleId,
                        hole_id_canonical: clickedHoleId,
                        total_depth: p.total_depth === null || p.total_depth === undefined ? null : Number(p.total_depth),
                        lat: null,
                        lng: null,
                        ore_bands: Number(p.ore_bands ?? 0),
                        ore_thickness_m: Number(p.ore_thickness_m ?? 0),
                    });
                });

                // ── Spiderfy ─────────────────────────────────────────────
                // Empty sources for the spider lines + spider points. When a
                // cluster is clicked and can't be zoomed further apart, we
                // populate these synthetic sources with displaced points
                // arranged in a circle around the cluster center.
                map.addSource('spider-lines', {
                    type: 'geojson',
                    data: { type: 'FeatureCollection', features: [] },
                });
                map.addSource('spider-points', {
                    type: 'geojson',
                    data: { type: 'FeatureCollection', features: [] },
                });
                map.addLayer({
                    id: 'spider-lines',
                    type: 'line',
                    source: 'spider-lines',
                    paint: {
                        'line-color': 'rgba(255,255,255,0.5)',
                        'line-width': 1.2,
                        'line-dasharray': [2, 2],
                    },
                });
                map.addLayer({
                    id: 'spider-halo',
                    type: 'circle',
                    source: 'spider-points',
                    filter: ['>', ['get', 'ore_bands'], 0],
                    paint: {
                        'circle-radius': 18,
                        'circle-color': '#7dd97c',
                        'circle-opacity': 0.35,
                        'circle-stroke-color': '#7dd97c',
                        'circle-stroke-width': 2,
                    },
                });
                map.addLayer({
                    id: 'spider-dot',
                    type: 'circle',
                    source: 'spider-points',
                    paint: {
                        'circle-radius': 11,
                        'circle-color': [
                            'case',
                            ['>', ['get', 'ore_bands'], 0], '#8fe28b',
                            '#7accee',
                        ],
                        'circle-stroke-color': '#0a0e14',
                        'circle-stroke-width': 2,
                    },
                });
                map.addLayer({
                    id: 'spider-label',
                    type: 'symbol',
                    source: 'spider-points',
                    layout: {
                        'text-field': ['get', 'hole_id'],
                        'text-size': 11,
                        'text-offset': [0, 1.2],
                        'text-anchor': 'top',
                        'text-allow-overlap': true,
                        'text-font': ['Open Sans Regular', 'Arial Unicode MS Regular'],
                    },
                    paint: {
                        'text-color': '#e8edf3',
                        'text-halo-color': '#0a0e14',
                        'text-halo-width': 1.5,
                    },
                });

                function collapseSpider() {
                    if (!map.getSource) return;
                    (map.getSource('spider-lines') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
                    (map.getSource('spider-points') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
                }

                function spiderfy(centerLngLat: [number, number], leaves: Array<{ properties: Record<string, unknown> }>) {
                    const centerPx = map.project(centerLngLat);
                    const n = leaves.length;
                    // Radius scales by sqrt(n) so big clusters spread to a
                    // wider ring (instead of all 60 leaves piling into a
                    // 95px circle and overlapping).
                    //   8 leaves  → ~70 px
                    //   18 leaves → ~106 px
                    //   60 leaves → ~194 px
                    const radius = Math.max(50, Math.sqrt(n) * 25);
                    const lineFeatures: unknown[] = [];
                    const pointFeatures: unknown[] = [];
                    for (let i = 0; i < n; i++) {
                        const angle = (i / n) * 2 * Math.PI - Math.PI / 2;
                        const px = centerPx.x + Math.cos(angle) * radius;
                        const py = centerPx.y + Math.sin(angle) * radius;
                        const ll = map.unproject([px, py]);
                        lineFeatures.push({
                            type: 'Feature',
                            geometry: { type: 'LineString', coordinates: [centerLngLat, [ll.lng, ll.lat]] },
                            properties: {},
                        });
                        pointFeatures.push({
                            type: 'Feature',
                            geometry: { type: 'Point', coordinates: [ll.lng, ll.lat] },
                            properties: leaves[i].properties,
                        });
                    }
                    (map.getSource('spider-lines') as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: lineFeatures });
                    (map.getSource('spider-points') as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: pointFeatures });
                    // eslint-disable-next-line no-console
                    console.log('[workspace-map] spiderfy populated', n, 'points at radius', radius);
                }

                map.on('click', 'cluster-circles', (e: {
                    features?: Array<{ geometry: { coordinates: [number, number] }; properties: Record<string, unknown> }>;
                }) => {
                    // eslint-disable-next-line no-console
                    console.log('[workspace-map] cluster-circles click', e.features?.length ?? 0, 'features');
                    const f = e.features?.[0];
                    if (!f) return;
                    const clusterId = Number(f.properties.cluster_id);
                    const center = f.geometry.coordinates;
                    // eslint-disable-next-line no-console
                    console.log('[workspace-map] cluster_id', clusterId, 'point_count', f.properties.point_count, 'center', center);
                    const src = map.getSource('collars') as {
                        getClusterLeaves: (id: number, limit: number, offset: number) => Promise<unknown[]>;
                    };
                    if (typeof src.getClusterLeaves !== 'function') {
                        // eslint-disable-next-line no-console
                        console.warn('[workspace-map] source has no getClusterLeaves — source is not clustered');
                        return;
                    }
                    // MapLibre v5 returns a Promise here (Mapbox GL JS used a
                    // callback). Using the callback signature silently
                    // produced no result.
                    src.getClusterLeaves(clusterId, 500, 0)
                        .then((leaves) => {
                            // eslint-disable-next-line no-console
                            console.log('[workspace-map] leaves count', leaves?.length ?? 0);
                            spiderfy(center, leaves as Array<{ properties: Record<string, unknown> }>);
                        })
                        .catch((err) => {
                            // eslint-disable-next-line no-console
                            console.warn('[workspace-map] getClusterLeaves err', err);
                        });
                });

                map.on('mouseenter', 'cluster-circles', () => { map.getCanvas().style.cursor = 'pointer'; });
                map.on('mouseleave', 'cluster-circles', () => { map.getCanvas().style.cursor = ''; });

                // Spider dot click — same payload shape as a regular collar
                // click. Auto-collapses the spider after handing the user the
                // popup so the layout doesn't stay cluttered.
                map.on('click', 'spider-dot', (e: { features?: Array<{ properties: Record<string, unknown> }> }) => {
                    const f = e.features?.[0];
                    if (!f) return;
                    const p = f.properties;
                    const clickedHoleId = String(p.hole_id);
                    const queued = compareSetRef.current;
                    if (queued.length === 1 && queued[0] !== clickedHoleId) {
                        onToggleCompare(clickedHoleId);
                        setActiveHoleRef.current(null);
                        collapseSpider();
                        return;
                    }
                    setActiveHoleRef.current({
                        collar_id: String(p.collar_id),
                        hole_id: clickedHoleId,
                        hole_id_canonical: clickedHoleId,
                        total_depth: p.total_depth === null || p.total_depth === undefined ? null : Number(p.total_depth),
                        lat: null,
                        lng: null,
                        ore_bands: Number(p.ore_bands ?? 0),
                        ore_thickness_m: Number(p.ore_thickness_m ?? 0),
                    });
                    collapseSpider();
                });
                map.on('mouseenter', 'spider-dot', () => { map.getCanvas().style.cursor = 'pointer'; });
                map.on('mouseleave', 'spider-dot', () => { map.getCanvas().style.cursor = ''; });

                // Spider stays visible while the user zooms/pans — collapsing
                // on every zoomstart was too aggressive and made the spider
                // feel like it never worked. Spider only collapses on:
                //   - spider-dot click (handler does it explicitly)
                //   - explicit empty-map click below
                //   - opening another cluster (its handler tears the old one
                //     down by overwriting the source data)
                map.on('click', (e: { point: { x: number; y: number } }) => {
                    const hits = map.queryRenderedFeatures(e.point, {
                        layers: ['cluster-circles', 'collars-dot', 'spider-dot'],
                    });
                    if (!hits || hits.length === 0) {
                        collapseSpider();
                    }
                });
            });

            mapRef.current = map;
        });

        return () => {
            cancelled = true;
            if (mapRef.current?.remove) {
                mapRef.current.remove();
            }
            mapRef.current = null;
        };
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [collars.length, projectSlug, basemap, registryStyle]);

    // React to layer toggle changes by updating MapLibre layer visibility +
    // filters on the existing map instance (don't tear down on every click).
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.getLayer) return;

        const setVis = (id: string, on: boolean) => {
            if (map.getLayer(id)) {
                map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
            }
        };

        const showCollars = visibleLayers.collars ?? true;
        const oreOnly = visibleLayers.samples ?? false;
        const heatmapOn = visibleLayers.ore_heatmap ?? false;
        const tracesOn = visibleLayers.traces ?? false;
        const aoiOn = visibleLayers.aoi ?? false;
        const tier5 = visibleLayers.tier_5 ?? false;
        const tier10 = visibleLayers.tier_10 ?? false;
        const tier20 = visibleLayers.tier_20 ?? false;

        setVis('collars-dot', showCollars);
        setVis('collars-halo', showCollars);
        setVis('collars-label', showCollars);
        setVis('collars-heatmap', heatmapOn);
        setVis('collar-traces-line', tracesOn);
        setVis('project-aoi-fill', aoiOn);
        setVis('project-aoi-line', aoiOn);

        // Compose the dot/halo/trace filter. Tier filters AND together with
        // each other (highest tier wins because it's strictest), and ore-only
        // narrows to ore_bands > 0.
        const conditions: unknown[] = [];
        if (oreOnly) {
            conditions.push(['>', ['get', 'ore_bands'], 0]);
        }
        let minThickness = 0;
        if (tier20) minThickness = Math.max(minThickness, 20);
        else if (tier10) minThickness = Math.max(minThickness, 10);
        else if (tier5) minThickness = Math.max(minThickness, 5);
        if (minThickness > 0) {
            conditions.push(['>=', ['get', 'ore_thickness_m'], minThickness]);
        }
        const filter = conditions.length === 0
            ? null
            : conditions.length === 1
                ? conditions[0]
                : ['all', ...conditions];

        if (map.getLayer('collars-dot')) {
            map.setFilter('collars-dot', filter);
        }
        if (map.getLayer('collar-traces-line')) {
            // Traces use the same filter as dots (so tier filters affect both)
            const traceFilter = minThickness > 0
                ? ['>=', ['get', 'ore_thickness_m'], minThickness]
                : null;
            map.setFilter('collar-traces-line', traceFilter);
        }
    }, [visibleLayers]);

    // Sync the compare-queue ring filter whenever the compareSet changes.
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.getLayer || !map.getLayer('collars-compare-ring')) return;
        map.setFilter('collars-compare-ring', [
            'in',
            ['get', 'hole_id'],
            ['literal', compareSet],
        ]);
    }, [compareSet]);

    // Terrain on/off — toggle map.setTerrain. The raster-dem source was
    // added on map load; we just flip whether MapLibre uses it for 3D.
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.getSource || !map.getSource('terrain-dem')) return;
        try {
            if (terrainOn) {
                map.setTerrain({ source: 'terrain-dem', exaggeration: 1.4 });
            } else {
                map.setTerrain(null);
            }
        } catch (e) {
            // eslint-disable-next-line no-console
            console.warn('[workspace-map] setTerrain failed', e);
        }
    }, [terrainOn]);

    // Tool mode — wire Pan (default, no-op) and Measure. Draw + Select
    // are next-phase. The effect attaches/detaches map handlers based on
    // the active tool so handlers don't compound.
    useEffect(() => {
        const map = mapRef.current;
        if (!map) return;

        // Default: ensure drag-pan is enabled so the user can move around.
        try { map.dragPan.enable(); } catch { /* noop */ }
        // Clear any leftover measure state when switching tools.
        const clearMeasure = () => {
            const src = map.getSource('measure-points') as { setData: (d: unknown) => void } | undefined;
            const lineSrc = map.getSource('measure-line') as { setData: (d: unknown) => void } | undefined;
            src?.setData({ type: 'FeatureCollection', features: [] });
            lineSrc?.setData({ type: 'FeatureCollection', features: [] });
        };
        clearMeasure();

        if (activeTool !== 'measure') {
            return;
        }

        // Measure mode: click to drop points, line+distance redraw with each.
        // Lazy-add sources/layers on first measure entry (idempotent).
        if (!map.getSource('measure-points')) {
            map.addSource('measure-points', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addSource('measure-line', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addLayer({
                id: 'measure-line',
                type: 'line',
                source: 'measure-line',
                paint: { 'line-color': '#e8a36b', 'line-width': 2.5, 'line-dasharray': [2, 2] },
            });
            map.addLayer({
                id: 'measure-dots',
                type: 'circle',
                source: 'measure-points',
                paint: {
                    'circle-radius': 5,
                    'circle-color': '#e8a36b',
                    'circle-stroke-color': '#0a0e14',
                    'circle-stroke-width': 1.5,
                },
            });
            map.addLayer({
                id: 'measure-labels',
                type: 'symbol',
                source: 'measure-points',
                layout: {
                    'text-field': ['get', 'label'],
                    'text-size': 11,
                    'text-offset': [0, -1.4],
                    'text-anchor': 'bottom',
                    'text-allow-overlap': true,
                    'text-font': ['Open Sans Regular', 'Arial Unicode MS Regular'],
                },
                paint: {
                    'text-color': '#fff',
                    'text-halo-color': '#0a0e14',
                    'text-halo-width': 1.5,
                },
            });
        }

        const points: Array<[number, number]> = [];
        const haversine = (a: [number, number], b: [number, number]) => {
            const R = 6371000;
            const toRad = (d: number) => (d * Math.PI) / 180;
            const dLat = toRad(b[1] - a[1]);
            const dLng = toRad(b[0] - a[0]);
            const φ1 = toRad(a[1]);
            const φ2 = toRad(b[1]);
            const x = Math.sin(dLat / 2) ** 2 + Math.cos(φ1) * Math.cos(φ2) * Math.sin(dLng / 2) ** 2;
            return 2 * R * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
        };

        function redraw() {
            let cumulative = 0;
            const pointFeatures = points.map((p, i) => {
                if (i > 0) cumulative += haversine(points[i - 1], p);
                const label = i === 0 ? '0 m' : cumulative >= 1000 ? `${(cumulative / 1000).toFixed(2)} km` : `${Math.round(cumulative)} m`;
                return {
                    type: 'Feature' as const,
                    geometry: { type: 'Point' as const, coordinates: p },
                    properties: { label },
                };
            });
            const lineFeature = points.length >= 2 ? [{
                type: 'Feature' as const,
                geometry: { type: 'LineString' as const, coordinates: points.slice() },
                properties: {},
            }] : [];
            (map.getSource('measure-points') as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: pointFeatures });
            (map.getSource('measure-line') as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: lineFeature });
        }

        function onClick(e: { lngLat: { lng: number; lat: number } }) {
            points.push([e.lngLat.lng, e.lngLat.lat]);
            redraw();
        }
        function onDblClick(e: { preventDefault: () => void }) {
            // Double-click ends the line; clear points so the next click
            // starts fresh.
            e.preventDefault();
            points.length = 0;
            redraw();
        }
        map.on('click', onClick);
        map.on('dblclick', onDblClick);
        map.getCanvas().style.cursor = 'crosshair';

        return () => {
            map.off('click', onClick);
            map.off('dblclick', onDblClick);
            map.getCanvas().style.cursor = '';
            // Don't clear measure data on tool exit — user might want to
            // keep the last reading visible. clearMeasure() above runs on
            // next entry to the effect.
        };
    }, [activeTool]);

    // Tool mode — Draw + Select (Phase 2).
    useEffect(() => {
        const map = mapRef.current;
        if (!map) return;

        // Clean prior tool layers' data so nothing stale lingers.
        const clearDraw = () => {
            (map.getSource('draw-polygon') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
            (map.getSource('draw-vertices') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
        };
        const clearSelect = () => {
            (map.getSource('select-highlight') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
            setSelectRect(null);
            setSelectedHoles([]);
        };

        if (activeTool !== 'draw' && activeTool !== 'select') {
            clearDraw();
            clearSelect();
            return;
        }

        // ── Draw polygon ──────────────────────────────────────────
        if (activeTool === 'draw') {
            clearSelect();
            if (!map.getSource('draw-polygon')) {
                map.addSource('draw-polygon', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
                map.addSource('draw-vertices', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
                map.addLayer({
                    id: 'draw-polygon-fill',
                    type: 'fill',
                    source: 'draw-polygon',
                    paint: { 'fill-color': '#e8a36b', 'fill-opacity': 0.12 },
                });
                map.addLayer({
                    id: 'draw-polygon-line',
                    type: 'line',
                    source: 'draw-polygon',
                    paint: { 'line-color': '#e8a36b', 'line-width': 2, 'line-dasharray': [3, 2] },
                });
                map.addLayer({
                    id: 'draw-vertices',
                    type: 'circle',
                    source: 'draw-vertices',
                    paint: {
                        'circle-radius': 5,
                        'circle-color': '#e8a36b',
                        'circle-stroke-color': '#0a0e14',
                        'circle-stroke-width': 1.5,
                    },
                });
            }

            const ring: Array<[number, number]> = [];

            const haversine = (a: [number, number], b: [number, number]) => {
                const R = 6371000;
                const toRad = (d: number) => (d * Math.PI) / 180;
                const dLat = toRad(b[1] - a[1]);
                const dLng = toRad(b[0] - a[0]);
                const φ1 = toRad(a[1]);
                const φ2 = toRad(b[1]);
                const x = Math.sin(dLat / 2) ** 2 + Math.cos(φ1) * Math.cos(φ2) * Math.sin(dLng / 2) ** 2;
                return 2 * R * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
            };

            function polygonAreaM2(pts: Array<[number, number]>): number {
                if (pts.length < 3) return 0;
                // Shoelace in lng/lat degrees, then rescale to meters
                // using local cosine for longitude shrink.
                let acc = 0;
                for (let i = 0; i < pts.length; i++) {
                    const j = (i + 1) % pts.length;
                    acc += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1];
                }
                const meanLat = pts.reduce((s, p) => s + p[1], 0) / pts.length;
                const sqMPerSqDeg = 111000 * 111000 * Math.cos((meanLat * Math.PI) / 180);
                return Math.abs(acc / 2) * sqMPerSqDeg;
            }

            function redrawDraw() {
                const vtxFeatures = ring.map((p) => ({
                    type: 'Feature' as const,
                    geometry: { type: 'Point' as const, coordinates: p },
                    properties: {},
                }));
                (map.getSource('draw-vertices') as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: vtxFeatures });
                if (ring.length >= 3) {
                    const closed = [...ring, ring[0]];
                    (map.getSource('draw-polygon') as { setData: (d: unknown) => void }).setData({
                        type: 'FeatureCollection',
                        features: [{
                            type: 'Feature',
                            geometry: { type: 'Polygon', coordinates: [closed] },
                            properties: {},
                        }],
                    });
                } else {
                    (map.getSource('draw-polygon') as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: [] });
                }
            }

            function onDrawClick(e: { lngLat: { lng: number; lat: number } }) {
                ring.push([e.lngLat.lng, e.lngLat.lat]);
                redrawDraw();
            }
            function onDrawDblClick(e: { preventDefault: () => void }) {
                e.preventDefault();
                if (ring.length >= 3) {
                    const areaM2 = polygonAreaM2(ring);
                    let perimM = 0;
                    for (let i = 0; i < ring.length; i++) {
                        perimM += haversine(ring[i], ring[(i + 1) % ring.length]);
                    }
                    const areaLabel = areaM2 >= 1_000_000 ? `${(areaM2 / 1_000_000).toFixed(3)} km²` : `${Math.round(areaM2).toLocaleString()} m² (${(areaM2 / 10_000).toFixed(2)} ha)`;
                    const perimLabel = perimM >= 1000 ? `${(perimM / 1000).toFixed(2)} km` : `${Math.round(perimM)} m`;
                    // eslint-disable-next-line no-alert
                    alert(`Polygon\nArea: ${areaLabel}\nPerimeter: ${perimLabel}\n${ring.length} vertices`);
                }
                ring.length = 0;
                redrawDraw();
            }
            map.on('click', onDrawClick);
            map.on('dblclick', onDrawDblClick);
            map.getCanvas().style.cursor = 'crosshair';
            return () => {
                map.off('click', onDrawClick);
                map.off('dblclick', onDrawDblClick);
                map.getCanvas().style.cursor = '';
            };
        }

        // ── Select (box) ─────────────────────────────────────────────
        if (activeTool === 'select') {
            clearDraw();
            if (!map.getSource('select-highlight')) {
                map.addSource('select-highlight', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
                map.addLayer({
                    id: 'select-highlight-ring',
                    type: 'circle',
                    source: 'select-highlight',
                    paint: {
                        'circle-radius': 14,
                        'circle-color': 'rgba(0,0,0,0)',
                        'circle-stroke-color': '#e8a36b',
                        'circle-stroke-width': 2.5,
                    },
                });
            }

            // Disable drag-pan so the box drag captures cleanly. Re-enabled
            // in cleanup.
            try { map.dragPan.disable(); } catch { /* noop */ }
            map.getCanvas().style.cursor = 'crosshair';

            let down: { x: number; y: number } | null = null;

            function getRectBounds(a: { x: number; y: number }, b: { x: number; y: number }) {
                return {
                    minX: Math.min(a.x, b.x),
                    minY: Math.min(a.y, b.y),
                    maxX: Math.max(a.x, b.x),
                    maxY: Math.max(a.y, b.y),
                };
            }

            function onDown(e: { point: { x: number; y: number }; originalEvent: MouseEvent }) {
                if (e.originalEvent.button !== 0) return;
                down = { x: e.point.x, y: e.point.y };
                setSelectRect({ x1: down.x, y1: down.y, x2: down.x, y2: down.y });
            }
            function onMove(e: { point: { x: number; y: number } }) {
                if (!down) return;
                setSelectRect({ x1: down.x, y1: down.y, x2: e.point.x, y2: e.point.y });
            }
            function onUp(e: { point: { x: number; y: number } }) {
                if (!down) return;
                const b = getRectBounds(down, e.point);
                down = null;
                const minDrag = 4;
                if (b.maxX - b.minX < minDrag || b.maxY - b.minY < minDrag) {
                    setSelectRect(null);
                    return;
                }
                // queryRenderedFeatures by screen box on the collars-dot
                // layer. Spider-dots intentionally excluded — the spider
                // is a transient cluster expansion, not a selection target.
                const feats = map.queryRenderedFeatures([[b.minX, b.minY], [b.maxX, b.maxY]], {
                    layers: ['collars-dot'],
                });
                const ids = Array.from(new Set((feats as Array<{ properties: { hole_id: string } }>).map((f) => String(f.properties.hole_id))));
                setSelectedHoles(ids);
                // Drop the highlight ring at each selected collar.
                const matchingPoints = collars.filter((c) => ids.includes(c.hole_id_canonical) && c.lat !== null && c.lng !== null);
                (map.getSource('select-highlight') as { setData: (d: unknown) => void }).setData({
                    type: 'FeatureCollection',
                    features: matchingPoints.map((c) => ({
                        type: 'Feature',
                        geometry: { type: 'Point', coordinates: [c.lng, c.lat] },
                        properties: { hole_id: c.hole_id_canonical },
                    })),
                });
                setSelectRect(null);
            }

            map.on('mousedown', onDown);
            map.on('mousemove', onMove);
            map.on('mouseup', onUp);
            return () => {
                map.off('mousedown', onDown);
                map.off('mousemove', onMove);
                map.off('mouseup', onUp);
                try { map.dragPan.enable(); } catch { /* noop */ }
                map.getCanvas().style.cursor = '';
            };
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeTool]);

    function jumpToLogs() {
        if (!activeHole) return;
        if (onJumpToLogs) {
            onJumpToLogs(activeHole.hole_id);
            return;
        }
        router.get(
            `/projects/${projectSlug}/workspace`,
            { log_hole: activeHole.hole_id },
            {
                preserveScroll: true,
                preserveState: true,
                only: [
                    'log_tracks',
                    'log_hole_id',
                    'log_depth_max',
                    'log_hole_total_depth',
                    'log_hole_easting',
                    'log_hole_northing',
                    'log_lithology_intervals',
                ],
            },
        );
    }

    const visibleCount = collars.filter((c) => c.lat !== null && c.lng !== null).length;
    const oreCount = collars.filter((c) => c.ore_bands > 0).length;

    return (
        <div style={{ position: 'relative', width: '100%', height, borderRadius: 6, overflow: 'hidden', border: '1px solid var(--line-1)' }}>
            <div ref={containerRef} style={{ width: '100%', height: '100%' }} />

            {/* Live drag-box overlay for Select tool. Pixel-positioned in
                the same screen space as the map canvas (since the parent
                wrapper is the map container). */}
            {selectRect && activeTool === 'select' && (
                <div
                    className="pointer-events-none absolute z-20 border-2"
                    style={{
                        left: Math.min(selectRect.x1, selectRect.x2),
                        top: Math.min(selectRect.y1, selectRect.y2),
                        width: Math.abs(selectRect.x2 - selectRect.x1),
                        height: Math.abs(selectRect.y2 - selectRect.y1),
                        borderColor: '#e8a36b',
                        background: 'rgba(232,163,107,0.10)',
                    }}
                />
            )}

            {/* Selection result panel — when Select tool has highlighted
                ≥1 hole. Surfaces the count + a few next-action buttons. */}
            {activeTool === 'select' && selectedHoles.length > 0 && (
                <div
                    className="absolute top-2 right-72 z-10 px-3 py-2 rounded border min-w-[240px] max-w-[300px]"
                    style={{ background: 'var(--bg-1)', borderColor: '#e8a36b', color: 'var(--fg-1)' }}
                >
                    <div className="flex items-start justify-between gap-2">
                        <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: '#e8a36b' }}>
                            Selection · {selectedHoles.length}
                        </div>
                        <button
                            type="button"
                            onClick={() => {
                                setSelectedHoles([]);
                                const map = mapRef.current;
                                (map?.getSource?.('select-highlight') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
                            }}
                            className="text-[10px] font-mono"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            ✕
                        </button>
                    </div>
                    <div className="mt-1 text-[11px] font-mono leading-snug" style={{ color: 'var(--fg-1)', maxHeight: 80, overflowY: 'auto' }}>
                        {selectedHoles.slice(0, 12).join(', ')}
                        {selectedHoles.length > 12 && ` … +${selectedHoles.length - 12} more`}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1">
                        <button
                            type="button"
                            disabled={selectedHoles.length < 2}
                            onClick={() => {
                                // Drop the first two into the compare queue.
                                onClearCompare();
                                onToggleCompare(selectedHoles[0]);
                                onToggleCompare(selectedHoles[1]);
                                onOpenCompare();
                            }}
                            className="flex-1 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border disabled:opacity-40"
                            style={{ color: '#e8a36b', borderColor: '#e8a36b', background: 'rgba(232,163,107,0.1)' }}
                        >
                            Compare first 2
                        </button>
                    </div>
                </div>
            )}

            {/* Tools segmented — top-right of the map, left of the
                MapLibre NavigationControl. Pan / Measure / Draw / Select. */}
            <div
                className="absolute top-2 right-12 z-10 flex flex-col items-end gap-1"
            >
                <div
                    className="inline-flex p-0.5 rounded border"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', backdropFilter: 'blur(4px)' }}
                >
                    {(['pan', 'measure', 'draw', 'select'] as MapTool[]).map((t) => {
                        const active = activeTool === t;
                        return (
                            <button
                                key={t}
                                type="button"
                                onClick={() => onToolChange(t)}
                                className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-wider rounded transition-colors"
                                style={{
                                    background: active ? '#e8a36b' : 'transparent',
                                    color: active ? '#0a0e14' : 'var(--fg-2)',
                                    fontWeight: active ? 600 : 400,
                                }}
                                title={
                                    t === 'pan' ? 'Pan — drag to move the map'
                                    : t === 'measure' ? 'Measure — click points; dblclick to reset'
                                    : t === 'draw' ? 'Draw — click vertices; dblclick to close polygon'
                                    : 'Select — drag a rectangle to highlight collars'
                                }
                            >
                                {t}
                            </button>
                        );
                    })}
                </div>
            </div>

            {/* Basemap + terrain selector — bottom-right of the map,
                above the scale control. Compact dropdown so it doesn't
                eat real estate. */}
            <div
                className="absolute bottom-2 right-2 z-10 flex items-center gap-2 text-[10px] font-mono px-2 py-1.5 rounded border"
                style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-2)' }}
            >
                <span className="uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Map</span>
                <select
                    value={basemap}
                    onChange={(e) => onBasemapChange(e.target.value as BasemapId)}
                    className="text-[10px] font-mono px-1.5 py-0.5 rounded border"
                    style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
                >
                    <option value="dark_matter">Dark</option>
                    <option value="positron">Light (Positron)</option>
                    <option value="bright">Bright (OSM)</option>
                    <option value="satellite">Satellite (Esri)</option>
                </select>
                <label className="flex items-center gap-1 cursor-pointer">
                    <input type="checkbox" checked={terrainOn} onChange={(e) => onTerrainChange(e.target.checked)} />
                    <span>3D terrain</span>
                </label>
            </div>

            {/* Tool affordance — small badge in the lower-left that shows
                which tool is active. Pan is the default; Measure surfaces
                a hint about the click/dblclick gestures. */}
            <div
                className="absolute bottom-2 left-2 z-10 text-[10px] font-mono px-2 py-1.5 rounded border flex items-center gap-2"
                style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-2)' }}
            >
                <span className="uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Tool</span>
                <span style={{ color: activeTool === 'pan' ? 'var(--fg-2)' : '#e8a36b' }}>
                    {activeTool === 'pan' && 'Pan (default · drag to move)'}
                    {activeTool === 'measure' && 'Measure (click points · dblclick to reset)'}
                    {activeTool === 'draw' && 'Draw (click vertices · dblclick to finish)'}
                    {activeTool === 'select' && (selectedHoles.length > 0 ? `Select · ${selectedHoles.length} hole${selectedHoles.length === 1 ? '' : 's'} highlighted` : 'Select (drag box on map)')}
                </span>
                {activeTool !== 'pan' && (
                    <button
                        type="button"
                        onClick={() => onToolChange('pan')}
                        className="text-[10px] font-mono px-1.5 py-0.5 rounded border"
                        style={{ borderColor: 'var(--line-2)', color: 'var(--fg-2)', background: 'var(--bg-2)' }}
                    >
                        Exit
                    </button>
                )}
            </div>

            {/* Project header overlay — sits below the Tools segmented
                control so the two don't overlap on the right edge. */}
            <div
                className="absolute right-12 z-10 px-3 py-2 rounded border max-w-[260px]"
                style={{ top: 44, background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-1)', backdropFilter: 'blur(6px)' }}
            >
                <div className="text-[10px] font-mono uppercase tracking-wider mb-0.5" style={{ color: 'var(--fg-3)' }}>
                    Project
                </div>
                <div className="text-xs font-medium leading-snug" style={{ color: 'var(--fg-0)' }}>
                    {projectInfo.project_name}
                </div>
                <div className="text-[10px] font-mono mt-1.5 space-y-0.5" style={{ color: 'var(--fg-2)' }}>
                    {projectInfo.company && <div>· {projectInfo.company}</div>}
                    {projectInfo.commodity && <div>· {projectInfo.commodity}</div>}
                    {projectInfo.region && <div>· {projectInfo.region}</div>}
                    {projectInfo.crs_epsg && <div style={{ color: 'var(--fg-3)' }}>EPSG:{projectInfo.crs_epsg}</div>}
                </div>
            </div>

            {/* Bottom stats chip — horizontally centered so it doesn't
                collide with the tool-affordance on the left or the
                basemap dropdown on the right. */}
            <div
                className="absolute bottom-2 left-1/2 -translate-x-1/2 text-[10px] font-mono px-3 py-1.5 rounded border z-10 flex flex-wrap gap-x-3 gap-y-0.5 justify-center"
                style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-2)', maxWidth: 'calc(100% - 32rem)' }}
            >
                <span>{visibleCount} collars</span>
                <span style={{ color: oreCount > 0 ? 'oklch(0.82 0.18 145)' : 'var(--fg-3)' }}>· {oreCount} with U-host</span>
                <span>· {projectSummary.total_drilled_m.toLocaleString()} m drilled</span>
                <span>· {projectSummary.total_ore_thickness_m.toFixed(1)} m derived ore</span>
                {projectSummary.mean_u3o8_pct !== null && (
                    <span>· mean {projectSummary.mean_u3o8_pct.toFixed(3)}% eU₃O₈</span>
                )}
            </div>

            {/* Hover tooltip */}
            {hoverHole && !activeHole && (
                <div
                    className="absolute z-10 pointer-events-none text-[10px] font-mono px-2 py-1 rounded border"
                    style={{
                        left: hoverHole.x + 12,
                        top: hoverHole.y + 12,
                        background: 'var(--bg-1)',
                        borderColor: 'var(--line-2)',
                        color: 'var(--fg-1)',
                    }}
                >
                    <div style={{ color: 'var(--fg-0)' }}>{hoverHole.hole.hole_id}</div>
                    {hoverHole.hole.total_depth !== null && (
                        <div style={{ color: 'var(--fg-3)' }}>TD {hoverHole.hole.total_depth.toFixed(1)} m</div>
                    )}
                    {hoverHole.hole.ore_bands > 0 ? (
                        <div style={{ color: 'oklch(0.82 0.18 145)' }}>
                            {hoverHole.hole.ore_bands} U bands · {hoverHole.hole.ore_thickness_m.toFixed(1)} m
                        </div>
                    ) : (
                        <div style={{ color: 'var(--fg-3)' }}>no derived U-host</div>
                    )}
                </div>
            )}

            {/* Compare queue banner — visible whenever ≥ 1 hole is queued.
                Sits top-right (above project header area) so the popup
                takes the left side. */}
            {compareSet.length > 0 && (
                <div
                    className="absolute top-2 left-72 z-10 px-3 py-2 rounded border max-w-[260px]"
                    style={{ background: 'var(--bg-1)', borderColor: '#e8a36b', color: 'var(--fg-1)' }}
                >
                    <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: '#e8a36b' }}>
                        Compare queue · {compareSet.length}/2
                    </div>
                    <div className="text-[11px] font-mono mb-2" style={{ color: 'var(--fg-1)' }}>
                        {compareSet.length === 1
                            ? <>{compareSet[0]} · <span style={{ color: 'var(--fg-3)' }}>click another collar to compare</span></>
                            : compareSet.join(' · ')}
                    </div>
                    <div className="flex gap-1">
                        <button
                            type="button"
                            disabled={compareSet.length < 2}
                            onClick={onOpenCompare}
                            className="flex-1 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border disabled:opacity-40"
                            style={{ color: '#e8a36b', borderColor: '#e8a36b', background: 'rgba(232,163,107,0.1)' }}
                        >
                            Open compare
                        </button>
                        <button
                            type="button"
                            onClick={onClearCompare}
                            className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                            style={{ color: 'var(--fg-3)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                        >
                            Clear
                        </button>
                    </div>
                </div>
            )}

            {/* Clicked hole detail — now anchored left so it's the dominant
                surface in the eye-tracking path, with the compare banner
                stacked to the right. */}
            {activeHole && (
                <div
                    className="absolute top-2 left-2 z-10 px-3 py-2 rounded border min-w-[240px] max-w-[260px]"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-2)', color: 'var(--fg-1)' }}
                >
                    <div className="flex items-start justify-between gap-2">
                        <div className="text-[11px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                            Hole
                        </div>
                        <button
                            type="button"
                            onClick={() => setActiveHole(null)}
                            className="text-[10px] font-mono"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            ✕
                        </button>
                    </div>
                    <div className="text-sm font-medium mt-0.5" style={{ color: 'var(--fg-0)' }}>
                        {activeHole.hole_id}
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-1 text-[11px]">
                        <div style={{ color: 'var(--fg-3)' }}>Total depth</div>
                        <div className="font-mono text-right" style={{ color: 'var(--fg-1)' }}>
                            {activeHole.total_depth !== null ? `${activeHole.total_depth.toFixed(1)} m` : '—'}
                        </div>
                        <div style={{ color: 'var(--fg-3)' }}>Derived ore bands</div>
                        <div className="font-mono text-right" style={{ color: activeHole.ore_bands > 0 ? '#8fe28b' : 'var(--fg-3)' }}>
                            {activeHole.ore_bands}
                        </div>
                        <div style={{ color: 'var(--fg-3)' }}>U-host thickness</div>
                        <div className="font-mono text-right" style={{ color: 'var(--fg-1)' }}>
                            {activeHole.ore_thickness_m > 0 ? `${activeHole.ore_thickness_m.toFixed(1)} m` : '—'}
                        </div>
                    </div>
                    {(() => {
                        const inSet = compareSet.includes(activeHole.hole_id);
                        const hint = inSet
                            ? 'queued · click any other collar to compare'
                            : compareSet.length === 0
                                ? 'check, then click another collar to compare'
                                : compareSet.length >= 2
                                    ? 'queue full — clear to add another'
                                    : 'add to queue';
                        return (
                            <label
                                className="mt-2 flex items-center gap-2 text-[11px] font-mono cursor-pointer px-2 py-1.5 rounded border"
                                style={{
                                    borderColor: inSet ? '#e8a36b' : 'var(--line-2)',
                                    background: inSet ? 'rgba(232,163,107,0.1)' : 'var(--bg-2)',
                                    color: inSet ? '#e8a36b' : 'var(--fg-2)',
                                }}
                            >
                                <input
                                    type="checkbox"
                                    checked={inSet}
                                    disabled={!inSet && compareSet.length >= 2}
                                    onChange={() => onToggleCompare(activeHole.hole_id)}
                                />
                                <span>Compare · <span style={{ color: inSet ? '#e8a36b' : 'var(--fg-3)' }}>{hint}</span></span>
                            </label>
                        );
                    })()}
                    <button
                        type="button"
                        onClick={jumpToLogs}
                        className="mt-2 w-full text-[10px] font-mono uppercase tracking-wider px-2 py-1.5 rounded border"
                        style={{ color: 'var(--accent)', borderColor: 'var(--accent-dim)', background: 'var(--accent-bg)' }}
                    >
                        View in LOGS →
                    </button>
                </div>
            )}
        </div>
    );
}
