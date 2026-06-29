import { useEffect, useImperativeHandle, useRef, forwardRef, useCallback, useState } from 'react';
import { usePage } from '@inertiajs/react';
import maplibregl, { LngLatBoundsLike, StyleSpecification, VectorTileSource } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { BboxGeoJson } from '@/Types/PublicGeoscience';
import { useBasemapStyleUrl } from '@/lib/basemap';
import { escapeHtml } from '@/lib/escapeHtml';
// Phase G.4 follow-up — subscribe to Evidence Map Mode so chat
// citations of type pg_feature highlight here.
import { useEvidenceMapPin } from '@/Hooks/useEvidenceMapPin';
// Phase 4 — workspace-global tile cache invalidation. PublicGeoscienceController
// seeds the initial epoch as a top-level Inertia prop (pgeo_jurisdiction_epoch);
// usePublicGeoscienceTileInvalidation updates it live when the
// public_geoscience_pull workflow ingests new features.
import { usePublicGeoscienceTileInvalidation } from '@/Hooks/useTileInvalidation';
import {
    LAYER_SPECS,
    type LayerId,
    type LayerSpec,
    pointLayers,
    polygonLayers,
    lineLayers,
    combineFilters,
    jurisdictionFilter,
    commodityGroupingFilter,
    MINE_STYLE,
    OCCURRENCE_STYLE,
    DRILLHOLE_STYLE,
    ROCK_SAMPLE_STYLE,
    SMDI_STYLE,
    SMDI_GROUPING_MATCH_EXPR,
    FAULT_STYLE,
    DYKE_STYLE,
    WELL_TRAJECTORY_STYLE,
    GENERIC_LINE_STYLE,
} from './publicGeoscienceLayers';

/**
 * Basemap IDs surfaced to the consumer. Mirrors the WorkspaceMap registry
 * (dark_matter | positron | bright | satellite) so the two map surfaces
 * share the same selector shape. 'satellite' is an inline raster style
 * (Esri World Imagery — free, no key) since useBasemapStyleUrl only
 * covers vector styles served by OpenFreeMap / Carto.
 */
export type BasemapId = 'dark_matter' | 'positron' | 'bright' | 'satellite';

/**
 * Map tool — mirrors the WorkspaceMap segmented control so geologists
 * carry the same muscle memory between project and public-geo surfaces.
 *   pan      — default; drag to move the map (no extra handlers)
 *   measure  — click to drop points, line + distance accumulates
 *   draw     — click vertices, dblclick to close + report area/perimeter
 *   select   — drag a rectangle, highlights every clickable feature in box
 */
export type MapTool = 'pan' | 'measure' | 'draw' | 'select';

const SATELLITE_STYLE: StyleSpecification = {
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
};

/**
 * Public Geoscience map — MapLibre basemap + four Martin MVT layers.
 *
 * Tile URLs route through the Laravel proxy at
 * /tiles/public-geoscience/{source}/{z}/{x}/{y}.pbf so SPA session auth
 * carries through on every request (plan §09a + §04g).
 *
 * Layer visibility + commodity grouping filter are driven from the parent
 * page so the right-rail LayerTogglePanel stays stateless.
 */

// Basemap style URL is config-driven via Inertia shared props. See
// resources/js/lib/basemap.ts and config/services.php basemap.styles. This
// lets on-prem deployments swap the public OpenFreeMap host for a self-
// hosted style.json without touching the component.

// Canada view — the landing state before a jurisdiction is selected.
const CANADA_CENTER: [number, number] = [-96.0, 56.0];
const CANADA_ZOOM = 2.6;

const TILE_URL_BASE = '/tiles/public-geoscience';

export interface PublicGeoscienceMapHandle {
    /** Fly the map to the envelope of a Polygon/MultiPolygon GeoJSON bbox. */
    fitBboxGeoJson: (bbox: BboxGeoJson) => void;
}

export interface PointPopup {
    layerId: LayerId;
    lngLat: [number, number];
    properties: Record<string, any>;
}

interface PublicGeoscienceMapProps {
    selectedLabel: string | null;
    /** Active jurisdiction code — scopes MVT rendering. */
    jurisdictionCode: string | null;
    /** Map of layer_id → visible boolean, driven by the right-rail toggles. */
    layerVisibility: Record<LayerId, boolean>;
    /** commodity_grouping filter applied across all layers; null = no filter. */
    commodityGrouping: string | null;
    /**
     * Selected basemap. Defaults to 'dark_matter' to match the Foundry
     * Workspace surface. Changing this prop swaps the style in place via
     * map.setStyle + a styledata reinstall of the MVT sources/layers
     * (preserves center/zoom unlike a teardown/rebuild).
     */
    basemap?: BasemapId;
    /**
     * Active map tool. Drives the in-map Tools segmented control + the
     * tool-specific click/drag handlers. Defaults to 'pan'.
     */
    activeTool?: MapTool;
    /** Tool-change callback for the in-map segmented control. */
    onToolChange?: (tool: MapTool) => void;
    /** Called when a user clicks a point or polygon feature. */
    onFeatureClick?: (popup: PointPopup) => void;
}

const PublicGeoscienceMap = forwardRef<
    PublicGeoscienceMapHandle,
    PublicGeoscienceMapProps
>(function PublicGeoscienceMap(
    {
        selectedLabel,
        jurisdictionCode,
        layerVisibility,
        commodityGrouping,
        basemap = 'dark_matter',
        activeTool = 'pan',
        onToolChange,
        onFeatureClick,
    },
    ref,
) {
    const containerRef = useRef<HTMLDivElement | null>(null);
    const mapRef = useRef<maplibregl.Map | null>(null);
    const readyRef = useRef<boolean>(false);
    const [mapLoaded, setMapLoaded] = useState<boolean>(false);

    // Phase 4 — PGEO tile cache-bust version.
    //
    // Seeded from PublicGeoscienceController's top-level
    // `pgeo_jurisdiction_epoch` prop (the same MAX(updated_at) epoch_s
    // value TileProxyController uses for its server-side ETag — so
    // client cache-bust and server ETag stay in lockstep on initial load).
    //
    // Updated live by usePublicGeoscienceTileInvalidation when the
    // public_geoscience_pull workflow successfully ingests new features.
    // Monotonic — only adopts higher values to ignore out-of-order events.
    const { props: pageProps } = usePage<{ pgeo_jurisdiction_epoch?: number }>();
    const [pgeoVersion, setPgeoVersion] = useState<number>(
        pageProps.pgeo_jurisdiction_epoch ?? 0,
    );

    usePublicGeoscienceTileInvalidation((newEpoch) => {
        setPgeoVersion((prev) => (newEpoch > prev ? newEpoch : prev));
    });

    // Stable ref so the version-change effect (which fires after mount)
    // can read the latest value without re-subscribing.
    const pgeoVersionRef = useRef<number>(pgeoVersion);
    useEffect(() => {
        pgeoVersionRef.current = pgeoVersion;
    }, [pgeoVersion]);
    // Bumped each time setStyle reinstalls the MVT sources/layers so the
    // visibility + filter effects re-apply against the fresh layer ids.
    const [styleEpoch, setStyleEpoch] = useState<number>(0);
    // Live drag-rect for the Select tool (screen-space pixel rect).
    const [selectRect, setSelectRect] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null);
    const [selectedFeatureIds, setSelectedFeatureIds] = useState<string[]>([]);
    // activeTool snapshot so the persistent feature-click handler (set up
    // once inside map.on('load')) can defer when a non-pan tool is active
    // without rebinding on every prop change.
    const activeToolRef = useRef<MapTool>(activeTool);
    useEffect(() => {
        activeToolRef.current = activeTool;
    }, [activeTool]);

    // Config-driven basemap URLs (per CLAUDE.md hard rule #8 / on-prem swap).
    // Resolve all three registered vector styles up front; satellite is an
    // inline raster style. The active style is picked at use sites.
    const darkStyleUrl = useBasemapStyleUrl('dark_matter');
    const positronStyleUrl = useBasemapStyleUrl('positron');
    const brightStyleUrl = useBasemapStyleUrl('bright');
    const styleFor = useCallback(
        (id: BasemapId): string | StyleSpecification =>
            id === 'dark_matter' ? darkStyleUrl
            : id === 'positron' ? positronStyleUrl
            : id === 'bright' ? brightStyleUrl
            : SATELLITE_STYLE,
        [darkStyleUrl, positronStyleUrl, brightStyleUrl],
    );

    // ── Map init (run once) ───────────────────────────────────────────
    useEffect(() => {
        if (!containerRef.current || mapRef.current) return;

        const map = new maplibregl.Map({
            container: containerRef.current,
            style: styleFor(basemap),
            center: CANADA_CENTER,
            zoom: CANADA_ZOOM,
            attributionControl: { compact: true },
            // The OpenFreeMap Positron style references font stacks
            // starting with "Open Sans Regular" but the OpenFreeMap
            // glyph server only serves "Noto Sans Regular". MapLibre
            // walks the font list looking for the first one that
            // returns 200 — rewriting the URL here skips the 404 round-
            // trip entirely and eliminates console noise. Only rewrites
            // the glyph resource type; every other request is untouched.
            transformRequest: (url, resourceType) => {
                if (
                    resourceType === 'Glyphs'
                    && url.includes('tiles.openfreemap.org/fonts/')
                    && url.includes('Open%20Sans%20Regular')
                ) {
                    return {
                        url: url.replace(
                            /Open%20Sans%20Regular[^/]*/,
                            'Noto%20Sans%20Regular',
                        ),
                    };
                }
                return { url };
            },
        });

        map.addControl(new maplibregl.NavigationControl(), 'top-right');
        map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

        map.on('load', () => {
            // Install the MVT sources + layers as soon as the basemap style
            // is ready. Filters get applied on the next prop effect pass.
            // Use the live version via ref so a tile-invalidation event that
            // fires between hook subscription and map.load doesn't lose its
            // cache-bust (rare but possible during a slow basemap fetch).
            installMvtSources(map, pgeoVersionRef.current);
            installMvtLayers(map);
            readyRef.current = true;
            setMapLoaded(true);

            // Click handler: pick the top-most feature from any clickable
            // layer and forward to the parent. Gated by activeToolRef so
            // measure/draw/select clicks don't also pop a feature card.
            map.on('click', (e) => {
                if (!onFeatureClick) return;
                if (activeToolRef.current !== 'pan') return;
                const features = map.queryRenderedFeatures(e.point, {
                    layers: CLICKABLE_LAYER_IDS,
                });
                if (features.length === 0) return;
                const feat = features[0];
                const [lng, lat] = (feat.geometry.type === 'Point')
                    ? (feat.geometry.coordinates as [number, number])
                    : [e.lngLat.lng, e.lngLat.lat];
                onFeatureClick({
                    layerId: inferLayerId(feat.layer.id),
                    lngLat: [lng, lat],
                    properties: (feat.properties ?? {}) as Record<string, any>,
                });
            });

            // Cursor affordance on hoverable layers.
            for (const layerId of CLICKABLE_LAYER_IDS) {
                map.on('mouseenter', layerId, () => {
                    map.getCanvas().style.cursor = 'pointer';
                });
                map.on('mouseleave', layerId, () => {
                    map.getCanvas().style.cursor = '';
                });
            }
        });

        mapRef.current = map;

        return () => {
            map.remove();
            mapRef.current = null;
            readyRef.current = false;
        };
        // onFeatureClick captured on first load only; React re-renders re-
        // bind the closure by reading the latest prop via the callback,
        // which we keep stable via useCallback on the page side.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // ── Swap basemap style in place ───────────────────────────────────
    // setStyle preserves view state (center/zoom/bearing/pitch) but wipes
    // every non-basemap source/layer. We reinstall the MVT stack on
    // 'style.load' — fires exactly once after the new basemap finishes
    // loading, so our layers land ON TOP of the new basemap layers
    // (correct draw order). Using 'styledata' instead fires too early,
    // before basemap layers are fully added, and the subsequent basemap
    // additions cover/displace the MVT layers — which manifests as the
    // data disappearing on basemap toggle. Skips the first mount
    // (init handles that path).
    const lastBasemapRef = useRef<BasemapId>(basemap);
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !readyRef.current) return;
        if (lastBasemapRef.current === basemap) return;
        lastBasemapRef.current = basemap;

        map.once('style.load', () => {
            installMvtSources(map, pgeoVersionRef.current);
            installMvtLayers(map);
            // Bump epoch so the visibility + filter effects re-apply
            // against the freshly-installed layer ids.
            setStyleEpoch((e) => e + 1);
        });
        map.setStyle(styleFor(basemap));
    }, [basemap, styleFor]);

    // ── Phase 4 — tile cache invalidation: bump every MVT source's tile
    //    URL with the new `?v={pgeoVersion}` cache-bust. MapLibre treats
    //    the new URL as a distinct tile address and re-fetches, dropping
    //    the stale in-memory cache. The Laravel proxy independently
    //    revalidates the ETag (derived from the same epoch), so a 304
    //    fires when nothing actually changed and a 200 when it did.
    //
    //    Skips on first render (`pgeoVersion === seed`) — the addSource
    //    above already used the correct URL on map init. Only re-fires
    //    when the hook bumps the version above the seed.
    const lastAppliedVersionRef = useRef<number>(pgeoVersion);
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !readyRef.current) return;
        if (pgeoVersion === lastAppliedVersionRef.current) return;
        lastAppliedVersionRef.current = pgeoVersion;

        for (const spec of LAYER_SPECS) {
            const src = map.getSource(spec.id);
            if (src && (src as VectorTileSource).setTiles) {
                (src as VectorTileSource).setTiles([pgeoTileUrl(spec.id, pgeoVersion)]);
            }
        }
    }, [pgeoVersion]);

    // ── Apply layer visibility toggles ────────────────────────────────
    // Gated on jurisdictionCode: when no jurisdiction is selected the
    // page is in its empty-state, so every MVT layer is forced
    // invisible regardless of the layerVisibility prop. The user toggles
    // in the right rail still update state — they just don't render
    // anything until a jurisdiction is chosen.
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !readyRef.current) return;

        const hasJurisdiction = !!jurisdictionCode;

        for (const spec of LAYER_SPECS) {
            const visible = hasJurisdiction && (layerVisibility[spec.id] ?? false);
            const ids = layerIdsFor(spec);
            for (const id of ids) {
                if (!map.getLayer(id)) continue;
                map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
            }
        }
    }, [layerVisibility, jurisdictionCode, styleEpoch]);

    // ── Apply jurisdiction + commodity filter ─────────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !readyRef.current) return;

        const filter = combineFilters(
            jurisdictionFilter(jurisdictionCode),
            commodityGroupingFilter(commodityGrouping),
        );

        for (const spec of LAYER_SPECS) {
            // SMDI is single-jurisdiction (SK) and preserves the upstream
            // SYMBOLOGY_GROUPING field verbatim — the canonical jurisdiction_code
            // / commodity_grouping filters (snake_case) don't apply to it.
            if (spec.id === 'smdi_deposits') continue;
            for (const id of layerIdsFor(spec)) {
                if (!map.getLayer(id)) continue;
                // MapLibre treats null filter as "clear filter".
                map.setFilter(id, filter ?? undefined);
            }
        }
    }, [jurisdictionCode, commodityGrouping, styleEpoch]);

    // ── Phase G.4 follow-up — Evidence Map Mode pin → PG-feature highlight ──
    //
    // When a chat citation marker resolves to a {kind:'pg_feature',
    // canonical_type, feature_id} pin via the Evidence Map Mode store,
    // attempt to:
    //   1. Find the MVT feature whose source_feature_id property
    //      matches feature_id (via queryRenderedFeatures across all
    //      visible PG layers).
    //   2. If found, fly to its geometry and add a brief highlight
    //      effect by emitting a popup at that location.
    //
    // Limitations: the feature must be currently rendered in the
    // viewport for queryRenderedFeatures to see it. If the feature is
    // outside the viewport we log + skip silently. A future iteration
    // could fetch the feature's bbox from the FastAPI side and fitBounds
    // before re-querying.
    const evidenceMapPin = useEvidenceMapPin();
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !readyRef.current) return;
        if (!evidenceMapPin || evidenceMapPin.kind !== 'pg_feature') return;

        const targetFeatureId = evidenceMapPin.feature_id;
        // queryRenderedFeatures across all MVT layers visible in the
        // current viewport. Match on `source_feature_id` (the canonical
        // identifier the FastAPI assembler emits).
        const allLayerIds = LAYER_SPECS.flatMap((s) => layerIdsFor(s))
            .filter((id) => !!map.getLayer(id));
        const matches = map.queryRenderedFeatures({ layers: allLayerIds })
            .filter((f) => {
                const props = f.properties ?? {};
                return (
                    props.source_feature_id === targetFeatureId
                    || props.feature_id === targetFeatureId
                    || props.id === targetFeatureId
                );
            });

        if (matches.length === 0) {
            // Feature isn't in the current viewport — best-effort.
            return;
        }

        const target = matches[0];
        const geom = target.geometry as
            | { type: 'Point'; coordinates: [number, number] }
            | { type: string; coordinates: unknown };
        if (geom?.type === 'Point' && Array.isArray(geom.coordinates)) {
            const [lon, lat] = geom.coordinates as [number, number];
            map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 11) });
            new maplibregl.Popup({ closeButton: true })
                .setLngLat([lon, lat])
                .setHTML(
                    `<div style="font-size:11px"><strong>Evidence pin</strong><br>`
                    + `${escapeHtml(targetFeatureId)}</div>`,
                )
                .addTo(map);
        }
    }, [evidenceMapPin]);

    // ── Map tool — Pan / Measure / Draw / Select ──────────────────────
    // Port of the same surface in Foundry/WorkspaceMap, adapted for PG:
    //   - Select queries CLICKABLE_LAYER_IDS (mines, occurrences,
    //     drillholes, samples, surveys, etc.) instead of just collars.
    //   - Result panel summarises the matched feature IDs rather than
    //     pushing into a compare queue.
    // Pan/Measure/Draw are domain-neutral and stay identical.
    useEffect(() => {
        const map = mapRef.current;
        if (!map) return;

        // Default: ensure drag-pan is enabled.
        try { map.dragPan.enable(); } catch { /* noop */ }

        // Clear any leftover measure/draw/select state.
        const clearMeasure = () => {
            (map.getSource?.('measure-points') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
            (map.getSource?.('measure-line') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
        };
        const clearDraw = () => {
            (map.getSource?.('draw-polygon') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
            (map.getSource?.('draw-vertices') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
        };
        const clearSelect = () => {
            (map.getSource?.('select-highlight') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
            setSelectRect(null);
            setSelectedFeatureIds([]);
        };

        if (activeTool === 'pan') {
            clearMeasure();
            clearDraw();
            clearSelect();
            map.getCanvas().style.cursor = '';
            return;
        }

        // ── Measure ──────────────────────────────────────────────────
        if (activeTool === 'measure') {
            clearDraw();
            clearSelect();
            if (!map.getSource('measure-points')) {
                map.addSource('measure-points', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
                map.addSource('measure-line',   { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
                map.addLayer({
                    id: 'measure-line', type: 'line', source: 'measure-line',
                    paint: { 'line-color': '#e8a36b', 'line-width': 2.5, 'line-dasharray': [2, 2] },
                });
                map.addLayer({
                    id: 'measure-dots', type: 'circle', source: 'measure-points',
                    paint: {
                        'circle-radius': 5,
                        'circle-color': '#e8a36b',
                        'circle-stroke-color': '#0a0e14',
                        'circle-stroke-width': 1.5,
                    },
                });
                map.addLayer({
                    id: 'measure-labels', type: 'symbol', source: 'measure-points',
                    layout: {
                        'text-field': ['get', 'label'],
                        'text-size': 11,
                        'text-offset': [0, -1.4],
                        'text-anchor': 'bottom',
                        'text-allow-overlap': true,
                        'text-font': ['Open Sans Regular', 'Noto Sans Regular', 'Arial Unicode MS Regular'],
                    },
                    paint: { 'text-color': '#fff', 'text-halo-color': '#0a0e14', 'text-halo-width': 1.5 },
                });
            }

            const points: Array<[number, number]> = [];
            function redraw() {
                if (!map) return;
                let cumulative = 0;
                const pointFeatures = points.map((p, i) => {
                    if (i > 0) cumulative += haversineM(points[i - 1], p);
                    const label = i === 0 ? '0 m' : cumulative >= 1000 ? `${(cumulative / 1000).toFixed(2)} km` : `${Math.round(cumulative)} m`;
                    return { type: 'Feature' as const, geometry: { type: 'Point' as const, coordinates: p }, properties: { label } };
                });
                const lineFeature = points.length >= 2 ? [{ type: 'Feature' as const, geometry: { type: 'LineString' as const, coordinates: points.slice() }, properties: {} }] : [];
                (map.getSource('measure-points') as unknown as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: pointFeatures });
                (map.getSource('measure-line') as unknown as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: lineFeature });
            }
            function onClick(e: maplibregl.MapMouseEvent) { points.push([e.lngLat.lng, e.lngLat.lat]); redraw(); }
            function onDbl(e: maplibregl.MapMouseEvent) { e.preventDefault(); points.length = 0; redraw(); }

            map.on('click', onClick);
            map.on('dblclick', onDbl);
            map.getCanvas().style.cursor = 'crosshair';
            return () => {
                map.off('click', onClick);
                map.off('dblclick', onDbl);
                map.getCanvas().style.cursor = '';
            };
        }

        // ── Draw polygon ─────────────────────────────────────────────
        if (activeTool === 'draw') {
            clearMeasure();
            clearSelect();
            if (!map.getSource('draw-polygon')) {
                map.addSource('draw-polygon',  { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
                map.addSource('draw-vertices', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
                map.addLayer({ id: 'draw-polygon-fill', type: 'fill', source: 'draw-polygon', paint: { 'fill-color': '#e8a36b', 'fill-opacity': 0.12 } });
                map.addLayer({ id: 'draw-polygon-line', type: 'line', source: 'draw-polygon', paint: { 'line-color': '#e8a36b', 'line-width': 2, 'line-dasharray': [3, 2] } });
                map.addLayer({
                    id: 'draw-vertices', type: 'circle', source: 'draw-vertices',
                    paint: {
                        'circle-radius': 5,
                        'circle-color': '#e8a36b',
                        'circle-stroke-color': '#0a0e14',
                        'circle-stroke-width': 1.5,
                    },
                });
            }
            const ring: Array<[number, number]> = [];
            function redrawDraw() {
                if (!map) return;
                const vtx = ring.map((p) => ({ type: 'Feature' as const, geometry: { type: 'Point' as const, coordinates: p }, properties: {} }));
                (map.getSource('draw-vertices') as unknown as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: vtx });
                if (ring.length >= 3) {
                    const closed = [...ring, ring[0]];
                    (map.getSource('draw-polygon') as unknown as { setData: (d: unknown) => void }).setData({
                        type: 'FeatureCollection',
                        features: [{ type: 'Feature', geometry: { type: 'Polygon', coordinates: [closed] }, properties: {} }],
                    });
                } else {
                    (map.getSource('draw-polygon') as unknown as { setData: (d: unknown) => void }).setData({ type: 'FeatureCollection', features: [] });
                }
            }
            function onClick(e: maplibregl.MapMouseEvent) { ring.push([e.lngLat.lng, e.lngLat.lat]); redrawDraw(); }
            function onDbl(e: maplibregl.MapMouseEvent) {
                e.preventDefault();
                if (ring.length >= 3) {
                    const areaM2 = polygonAreaM2(ring);
                    let perimM = 0;
                    for (let i = 0; i < ring.length; i++) perimM += haversineM(ring[i], ring[(i + 1) % ring.length]);
                    const areaLabel = areaM2 >= 1_000_000 ? `${(areaM2 / 1_000_000).toFixed(3)} km²` : `${Math.round(areaM2).toLocaleString()} m² (${(areaM2 / 10_000).toFixed(2)} ha)`;
                    const perimLabel = perimM >= 1000 ? `${(perimM / 1000).toFixed(2)} km` : `${Math.round(perimM)} m`;
                    // eslint-disable-next-line no-alert
                    alert(`Polygon\nArea: ${areaLabel}\nPerimeter: ${perimLabel}\n${ring.length} vertices`);
                }
                ring.length = 0;
                redrawDraw();
            }
            map.on('click', onClick);
            map.on('dblclick', onDbl);
            map.getCanvas().style.cursor = 'crosshair';
            return () => {
                map.off('click', onClick);
                map.off('dblclick', onDbl);
                map.getCanvas().style.cursor = '';
            };
        }

        // ── Select (drag box) ────────────────────────────────────────
        if (activeTool === 'select') {
            clearMeasure();
            clearDraw();
            if (!map.getSource('select-highlight')) {
                map.addSource('select-highlight', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
                map.addLayer({
                    id: 'select-highlight-ring', type: 'circle', source: 'select-highlight',
                    paint: {
                        'circle-radius': 14,
                        'circle-color': 'rgba(0,0,0,0)',
                        'circle-stroke-color': '#e8a36b',
                        'circle-stroke-width': 2.5,
                    },
                });
            }
            try { map.dragPan.disable(); } catch { /* noop */ }
            map.getCanvas().style.cursor = 'crosshair';

            let down: { x: number; y: number } | null = null;
            function bbox(a: { x: number; y: number }, b: { x: number; y: number }) {
                return { minX: Math.min(a.x, b.x), minY: Math.min(a.y, b.y), maxX: Math.max(a.x, b.x), maxY: Math.max(a.y, b.y) };
            }
            function onDown(e: maplibregl.MapMouseEvent) {
                if (e.originalEvent.button !== 0) return;
                down = { x: e.point.x, y: e.point.y };
                setSelectRect({ x1: down.x, y1: down.y, x2: down.x, y2: down.y });
            }
            function onMove(e: maplibregl.MapMouseEvent) {
                if (!down) return;
                setSelectRect({ x1: down.x, y1: down.y, x2: e.point.x, y2: e.point.y });
            }
            function onUp(e: maplibregl.MapMouseEvent) {
                if (!down) return;
                if (!map) return;
                const b = bbox(down, e.point);
                down = null;
                if (b.maxX - b.minX < 4 || b.maxY - b.minY < 4) {
                    setSelectRect(null);
                    return;
                }
                const feats = map.queryRenderedFeatures(
                    [[b.minX, b.minY], [b.maxX, b.maxY]] as [maplibregl.PointLike, maplibregl.PointLike],
                    { layers: CLICKABLE_LAYER_IDS },
                );
                const ids: string[] = [];
                const points: Array<[number, number]> = [];
                const seen = new Set<string>();
                for (const f of feats) {
                    const p = (f.properties ?? {}) as Record<string, unknown>;
                    const id = String(p.source_feature_id ?? p.feature_id ?? p.name ?? p.drillhole_name ?? p.drillhole_id ?? '');
                    if (!id || seen.has(id)) continue;
                    seen.add(id);
                    ids.push(id);
                    if (f.geometry?.type === 'Point' && Array.isArray((f.geometry as { coordinates: unknown }).coordinates)) {
                        points.push((f.geometry as unknown as { coordinates: [number, number] }).coordinates);
                    }
                }
                setSelectedFeatureIds(ids);
                (map.getSource('select-highlight') as unknown as { setData: (d: unknown) => void }).setData({
                    type: 'FeatureCollection',
                    features: points.map((c) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: c }, properties: {} })),
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
    }, [activeTool, mapLoaded]);

    // ── Imperative bbox fit exposed to parent ─────────────────────────
    useImperativeHandle(ref, () => ({
        fitBboxGeoJson: (bbox: BboxGeoJson) => {
            const map = mapRef.current;
            if (!map) return;

            const bounds = computeBounds(bbox);
            if (!bounds) return;

            const run = () =>
                map.fitBounds(bounds, {
                    padding: 60,
                    maxZoom: 7,
                    duration: 900,
                });

            if (readyRef.current) run();
            else map.once('load', run);
        },
    }));

    // ── transformRequest hook — no-op for same-origin, but kept as the
    // insertion point if Martin ever needs a Bearer token header. Today
    // /tiles/ is same-origin + session-authenticated.
    const _transformRequest = useCallback((url: string) => ({ url }), []);
    void _transformRequest;

    return (
        <div className="absolute inset-0">
            {/* MapLibre overrides the container's `position` to `relative`
                on init, so we can't use `absolute inset-0` on the container
                itself (inset doesn't determine size on relative elements).
                Instead the container uses `w-full h-full` — the parent
                wrapper's absolute positioning gives it resolved pixel
                dimensions, so 100% height resolves correctly. */}
            <div
                ref={containerRef}
                className="w-full h-full"
                aria-label="Public Geoscience map"
            />
            {/* Rec #5: loading skeleton while MapLibre initialises + basemap
                tiles download. Fades out once the style 'load' event fires. */}
            {!mapLoaded && (
                <div className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-gray-950/80 pointer-events-none">
                    <div className="w-8 h-8 rounded-full border-2 border-gray-700 border-t-amber-400 animate-spin" />
                    <p className="mt-3 text-xs text-gray-500 font-mono">Loading map tiles...</p>
                </div>
            )}
            {/* selectedLabel chip — only renders if the parent passes a
                non-null value. Default in our Foundry page is null
                (PageHeader + Card title carry that info instead). */}
            {selectedLabel && (
                <div
                    className="absolute top-3 left-3 z-10 px-3 py-1.5 rounded border text-xs font-mono"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-2)', color: 'var(--fg-1)' }}
                >
                    {selectedLabel}
                </div>
            )}

            {/* Tools segmented — top-right of the map, mirrors WorkspaceMap.
                Left of the MapLibre NavigationControl (which lives in the
                map's own top-right). */}
            {onToolChange && (
                <div className="absolute top-2 right-12 z-10">
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
                                        : 'Select — drag a rectangle to highlight features'
                                    }
                                >
                                    {t}
                                </button>
                            );
                        })}
                    </div>
                </div>
            )}

            {/* Drag-box overlay for Select. Pixel-positioned in the same
                screen space as the map canvas. */}
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

            {/* Tool status badge — bottom-left of the map. Mirrors
                Workspace's tool affordance. Pan = quiet default; other
                tools light up the accent. */}
            {onToolChange && (
                <div
                    className="absolute bottom-2 left-2 z-10 text-[10px] font-mono px-2 py-1.5 rounded border flex items-center gap-2"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-2)' }}
                >
                    <span className="uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Tool</span>
                    <span style={{ color: activeTool === 'pan' ? 'var(--fg-2)' : '#e8a36b' }}>
                        {activeTool === 'pan' && 'Pan (default · drag to move)'}
                        {activeTool === 'measure' && 'Measure (click points · dblclick to reset)'}
                        {activeTool === 'draw' && 'Draw (click vertices · dblclick to finish)'}
                        {activeTool === 'select' && (selectedFeatureIds.length > 0
                            ? `Select · ${selectedFeatureIds.length} feature${selectedFeatureIds.length === 1 ? '' : 's'} highlighted`
                            : 'Select (drag box on map)')}
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
            )}

            {/* Selection result panel — shown while Select has matches.
                Top-right under the tools segment so popups can take the
                left side (matching Workspace). */}
            {activeTool === 'select' && selectedFeatureIds.length > 0 && (
                <div
                    className="absolute top-12 right-12 z-10 px-3 py-2 rounded border min-w-[240px] max-w-[300px]"
                    style={{ background: 'var(--bg-1)', borderColor: '#e8a36b', color: 'var(--fg-1)' }}
                >
                    <div className="flex items-start justify-between gap-2">
                        <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: '#e8a36b' }}>
                            Selection · {selectedFeatureIds.length}
                        </div>
                        <button
                            type="button"
                            onClick={() => {
                                setSelectedFeatureIds([]);
                                const map = mapRef.current;
                                (map?.getSource?.('select-highlight') as { setData: (d: unknown) => void } | undefined)?.setData({ type: 'FeatureCollection', features: [] });
                            }}
                            className="text-[10px] font-mono"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            ✕
                        </button>
                    </div>
                    <div
                        className="mt-1 text-[11px] font-mono leading-snug"
                        style={{ color: 'var(--fg-1)', maxHeight: 100, overflowY: 'auto' }}
                    >
                        {selectedFeatureIds.slice(0, 16).join(', ')}
                        {selectedFeatureIds.length > 16 && ` … +${selectedFeatureIds.length - 16} more`}
                    </div>
                </div>
            )}
        </div>
    );
});

export default PublicGeoscienceMap;

// ── Measure / draw helpers ──────────────────────────────────────────────

/** Great-circle distance between two [lng, lat] points in metres. */
function haversineM(a: [number, number], b: [number, number]): number {
    const R = 6371000;
    const toRad = (d: number) => (d * Math.PI) / 180;
    const dLat = toRad(b[1] - a[1]);
    const dLng = toRad(b[0] - a[0]);
    const φ1 = toRad(a[1]);
    const φ2 = toRad(b[1]);
    const x = Math.sin(dLat / 2) ** 2 + Math.cos(φ1) * Math.cos(φ2) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
}

/**
 * Planar polygon area in m², using the shoelace formula in lng/lat
 * degrees rescaled by the local cosine-of-latitude shrink for longitude.
 * Good enough for AOI-scale polygons; not for hemisphere-scale ones.
 */
function polygonAreaM2(pts: Array<[number, number]>): number {
    if (pts.length < 3) return 0;
    let acc = 0;
    for (let i = 0; i < pts.length; i++) {
        const j = (i + 1) % pts.length;
        acc += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1];
    }
    const meanLat = pts.reduce((s, p) => s + p[1], 0) / pts.length;
    const sqMPerSqDeg = 111000 * 111000 * Math.cos((meanLat * Math.PI) / 180);
    return Math.abs(acc / 2) * sqMPerSqDeg;
}

// ── MVT installation helpers ────────────────────────────────────────────

/**
 * Build a PGEO tile URL template for a given source.
 *
 * The `?v={epoch}` cache-bust query keys the browser HTTP cache + the
 * Martin internal cache + (indirectly) the TileProxyController ETag.
 * Every time the underlying public_geo.* data changes, the broadcast
 * pump bumps `epoch` and a setTiles() with the new URL triggers a real
 * refetch instead of returning the stale cached body.
 */
function pgeoTileUrl(sourceId: string, version: number): string {
    return `${window.location.origin}${TILE_URL_BASE}/${sourceId}/{z}/{x}/{y}.pbf?v=${version}`;
}

function installMvtSources(map: maplibregl.Map, version: number): void {
    for (const spec of LAYER_SPECS) {
        if (map.getSource(spec.id)) continue;
        map.addSource(spec.id, {
            type: 'vector',
            tiles: [pgeoTileUrl(spec.id, version)],
            minzoom: 0,
            maxzoom: 14,
        });
    }
}

function installMvtLayers(map: maplibregl.Map): void {
    for (const spec of LAYER_SPECS) {
        const layers = buildLayersForSpec(spec);
        for (const layer of layers) {
            if (map.getLayer(layer.id)) continue;
            map.addLayer(layer as any);
        }
    }
}

function buildLayersForSpec(spec: LayerSpec): any[] {
    if (spec.kind === 'polygon') {
        return polygonLayers({
            sourceId: spec.id,
            sourceLayerName: spec.sourceLayer,
            idPrefix: spec.id,
        });
    }
    if (spec.kind === 'line') {
        // Per-source style routing — keep the switch here (not in
        // publicGeoscienceLayers.ts) so the LayerId union stays the one
        // source of truth and adding a new line source is one line of code.
        const lineStyle =
            spec.id === 'pg_geological_faults' ? FAULT_STYLE
            : spec.id === 'pg_geological_dykes' ? DYKE_STYLE
            : spec.id === 'pg_petroleum_well_trajectories' ? WELL_TRAJECTORY_STYLE
            : GENERIC_LINE_STYLE;
        return lineLayers({
            sourceId: spec.id,
            sourceLayerName: spec.sourceLayer,
            idPrefix: spec.id,
            style: lineStyle,
        });
    }
    // spec.kind === 'point'
    const style =
        spec.id === 'pg_mines'
            ? MINE_STYLE
            : spec.id === 'pg_mineral_occurrences'
                ? OCCURRENCE_STYLE
                : spec.id === 'pg_rock_samples'
                    ? ROCK_SAMPLE_STYLE
                    : spec.id === 'smdi_deposits'
                        ? SMDI_STYLE
                        : DRILLHOLE_STYLE;

    const noLabels = new Set(['pg_drillhole_collars', 'pg_rock_samples']);
    const withLabels = !noLabels.has(spec.id);
    const labelField =
        spec.id === 'pg_drillhole_collars' ? 'drillhole_name'
        : spec.id === 'pg_rock_samples' ? 'station'
        : 'name';

    // SMDI's standalone table preserves the upstream TitleCase
    // SYMBOLOGY_GROUPING field verbatim; every other point layer uses
    // the snake_case commodity_grouping from the canonical lakehouse.
    const colorExpr = spec.id === 'smdi_deposits' ? SMDI_GROUPING_MATCH_EXPR : undefined;

    return pointLayers({
        sourceId: spec.id,
        sourceLayerName: spec.sourceLayer,
        idPrefix: spec.id,
        style,
        withLabels,
        labelField,
        colorExpr,
    });
}

function layerIdsFor(spec: LayerSpec): string[] {
    if (spec.kind === 'polygon') return [`${spec.id}_fill`, `${spec.id}_outline`];
    if (spec.kind === 'line') return [`${spec.id}_casing`, `${spec.id}_line`];
    const ids = [`${spec.id}_heatmap`];
    // Point layers that emit a `_halo` sub-layer below the dot. Keep
    // this set in lockstep with the styles in publicGeoscienceLayers.ts
    // that set `withHalo: true` or `haloColor`.
    const withHalo = new Set([
        'pg_drillhole_collars',
        'pg_mines',
        'pg_mineral_occurrences',
        'pg_rock_samples',
        'smdi_deposits',
    ]);
    if (withHalo.has(spec.id)) ids.push(`${spec.id}_halo`);
    ids.push(`${spec.id}_circle`);
    // Labels are too dense on drillholes and rock samples at any zoom.
    const noLabels = new Set(['pg_drillhole_collars', 'pg_rock_samples']);
    if (!noLabels.has(spec.id)) ids.push(`${spec.id}_label`);
    return ids;
}

function inferLayerId(mapLayerId: string): LayerId {
    for (const spec of LAYER_SPECS) {
        if (mapLayerId.startsWith(spec.id)) return spec.id;
    }
    return 'pg_mines';
}

const CLICKABLE_LAYER_IDS: string[] = LAYER_SPECS.flatMap((spec) => {
    if (spec.kind === 'polygon') return [`${spec.id}_fill`];
    if (spec.kind === 'line') return [`${spec.id}_line`];
    return [`${spec.id}_circle`];
});

// ── Bounds helpers (unchanged from Phase 1) ─────────────────────────────

function computeBounds(bbox: BboxGeoJson): LngLatBoundsLike | null {
    const rings = flattenRings(bbox);
    if (rings.length === 0) return null;

    let minLng = Infinity;
    let minLat = Infinity;
    let maxLng = -Infinity;
    let maxLat = -Infinity;

    for (const [lng, lat] of rings) {
        if (lng < minLng) minLng = lng;
        if (lat < minLat) minLat = lat;
        if (lng > maxLng) maxLng = lng;
        if (lat > maxLat) maxLat = lat;
    }

    if (!isFinite(minLng) || !isFinite(minLat)) return null;

    return [
        [minLng, minLat],
        [maxLng, maxLat],
    ];
}

function flattenRings(bbox: BboxGeoJson): [number, number][] {
    const out: [number, number][] = [];
    if (bbox.type === 'Polygon') {
        const coords = bbox.coordinates as number[][][];
        for (const ring of coords) {
            for (const pt of ring) out.push([pt[0], pt[1]]);
        }
    } else if (bbox.type === 'MultiPolygon') {
        const coords = bbox.coordinates as number[][][][];
        for (const polygon of coords) {
            for (const ring of polygon) {
                for (const pt of ring) out.push([pt[0], pt[1]]);
            }
        }
    }
    return out;
}
