import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Head, usePage } from '@inertiajs/react';
import * as maplibregl from 'maplibre-gl';
import { configureMaplibreWorker } from '@/lib/maplibreWorker';
import type { Map as MapLibreMap, GeoJSONSource, AddLayerObject, FilterSpecification } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { PageHeader } from '@/Components/Foundry/primitives';
import { BASEMAP_OPTIONS, useBasemapStyleSpec, type BasemapId } from '@/lib/basemap';
import {
    PUBLIC_GEO_LAYER_LABELS,
    PUBLIC_GEO_LAYER_COLORS,
    type PublicGeoFeature,
    type PublicGeoFeatureCollection,
} from '@/Components/MapView';
import PolygonLayerToggles from '@/Components/PublicGeoscience/PolygonLayerToggles';
import PublicGeoSyncControls from '@/Components/PublicGeoscience/PublicGeoSyncControls';
import {
    DEFAULT_POLYGON_LAYERS,
    POLYGON_COLOR_MATCH,
    type PolygonFeatureProperties,
    type PolygonLayerKey,
    type PolygonResponseFields,
} from '@/Components/PublicGeoscience/polygonLayers';
import PublicGeoFeatureCard, { type PublicGeoSelection } from '@/Components/PublicGeoscience/PublicGeoFeatureCard';
import type { PageProps } from '@/types';

const SOURCE_ID = 'public-geoscience';
const POINT_LAYER_ID = 'public-geoscience-points';
const SELECTED_LAYER_ID = 'public-geoscience-selected';
const CLUSTER_LAYER_ID = 'public-geoscience-clusters';
const CLUSTER_COUNT_LAYER_ID = 'public-geoscience-cluster-counts';
const POLYGON_SOURCE_ID = 'public-geoscience-polygons';
const POLYGON_FILL_LAYER_ID = 'public-geoscience-polygon-fill';
const POLYGON_LINE_LAYER_ID = 'public-geoscience-polygon-line';

type MapResponse = PublicGeoFeatureCollection & PolygonResponseFields;

/** How long the map must sit still before we re-query. */
const MOVE_DEBOUNCE_MS = 350;

const LAYER_COLOR_MATCH = [
    'match',
    ['get', 'layer'],
    'mine',
    PUBLIC_GEO_LAYER_COLORS.mine,
    'mineral_occurrence',
    PUBLIC_GEO_LAYER_COLORS.mineral_occurrence,
    'drillhole_collar',
    PUBLIC_GEO_LAYER_COLORS.drillhole_collar,
    'rock_sample',
    PUBLIC_GEO_LAYER_COLORS.rock_sample,
    '#9ca3af',
];

interface Viewport {
    bbox: string;
    zoom: number;
}

/** Filter for the selected-record ring: the clicked point's id, or nothing. */
function selectedFilter(selection: PublicGeoSelection | null): FilterSpecification {
    const id = selection?.kind === 'point' ? selection.id : '';
    return ['all', ['!=', ['get', 'cluster'], true], ['==', ['get', 'id'], id]] as unknown as FilterSpecification;
}

type HoverTip = { title: string; sub: string; x: number; y: number };

/**
 * Foundry/PublicGeoscience — standalone browse page for /public-geoscience,
 * linked from the top ORG nav bar.
 *
 * 2026-08-19 — reworked from a fetch-everything-once page into a
 * viewport-driven one, alongside the PublicGeoscienceMapController rewrite.
 * The original fetched the whole endpoint on mount and re-fetched only when
 * the jurisdiction filter changed, which was fine against the ~29 rows the
 * old controller assumed and untenable against the real corpus (412,537
 * mineral occurrences alone). Now:
 *
 *   - the current bbox + zoom go to the server on every settled map move,
 *   - dense layers come back grid-aggregated and render as sized, counted
 *     cluster bubbles rather than as an arbitrary 2,000-row subset,
 *   - the header reports `total_in_view` (true record count) rather than
 *     the number of features drawn, and says so explicitly when the two
 *     differ, so "1,240 features" can never be mistaken for "that's all
 *     there is".
 *
 * See PublicGeoscienceMapController for the data scope and for the
 * empty-in-production caveat.
 *
 * 2026-09-29 — polygon overlays and operator controls:
 *
 *   - the four polygon tables (tenure, resource potential, assessment
 *     surveys, bedrock) are toggleable fill + outline layers, requested via
 *     `layers=` and drawn UNDER the points; tenure is on by default. Each
 *     layer's legend entry says when the server capped it or wants a closer
 *     zoom. Clicking a polygon opens a popup with its key attributes and the
 *     source's licence.
 *   - a freshness line (rows + last sync) and, for admins only, "Sync now",
 *     which queues the public_geo_sync Hatchet workflow and toasts its run id.
 *
 * 2026-09-29 (later) — consistent with the Workspace map:
 *
 *   - clicking a point opens PublicGeoFeatureCard, the same top-left card
 *     the Workspace map uses for a clicked collar. For a public drillhole
 *     it shows the full record — depth, dip/azimuth, operator, project,
 *     core, and the published stratigraphic contacts drawn down the hole.
 *     Before this a point click did nothing at all; only clusters answered.
 *   - polygons open the same card instead of a MapLibre popup;
 *   - hover is the Workspace's DOM tooltip, not a dark MapLibre popup;
 *   - the basemap picker (Dark default, Light, Bright, Satellite) is the
 *     Workspace's, and points use its zoom-scaled collar dot.
 */
export default function PublicGeoscience() {
    const mapContainer = useRef<HTMLDivElement | null>(null);
    const mapRef = useRef<MapLibreMap | null>(null);
    const [hover, setHover] = useState<HoverTip | null>(null);
    const [selection, setSelection] = useState<PublicGeoSelection | null>(null);
    const selectionRef = useRef<PublicGeoSelection | null>(null);
    useEffect(() => {
        selectionRef.current = selection;
    }, [selection]);
    // The camera survives a basemap switch, which rebuilds the map.
    const cameraRef = useRef<{ center: [number, number]; zoom: number }>({ center: [-107, 55], zoom: 4 });
    const [mapReady, setMapReady] = useState(false);
    const [data, setData] = useState<MapResponse | null>(null);
    const [polygonLayers, setPolygonLayers] = useState<PolygonLayerKey[]>(DEFAULT_POLYGON_LAYERS);
    const isAdmin = Boolean(usePage<PageProps>().props.auth?.user?.is_admin);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [jurisdiction, setJurisdiction] = useState('');
    const [viewport, setViewport] = useState<Viewport | null>(null);

    // Dark by default, like the Workspace map.
    const [basemap, setBasemap] = useState<BasemapId>('dark_matter');
    const styleSpec = useBasemapStyleSpec(basemap);

    // The click handler is bound once per map; it reads the latest response
    // (for source attribution) through a ref rather than re-binding per fetch.
    const dataRef = useRef<MapResponse | null>(null);
    useEffect(() => {
        dataRef.current = data;
    }, [data]);

    // Jurisdiction codes accumulate across fetches instead of being derived
    // from the current response. Deriving them per-response would make the
    // dropdown's options change as you pan — and selecting a jurisdiction
    // narrows the response, which would then drop every other option out of
    // the list you just used.
    const [seenJurisdictions, setSeenJurisdictions] = useState<string[]>([]);

    const readViewport = useCallback((map: MapLibreMap): Viewport => {
        const b = map.getBounds();
        return {
            bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((n) => n.toFixed(5)).join(','),
            zoom: Math.round(map.getZoom() * 10) / 10,
        };
    }, []);

    // ── Init map once ───────────────────────────────────────────────────────
    useEffect(() => {
        if (!mapContainer.current) return;

        configureMaplibreWorker(maplibregl);
        const map = new maplibregl.Map({
            container: mapContainer.current,
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            style: styleSpec as any,
            center: cameraRef.current.center,
            zoom: cameraRef.current.zoom,
        });

        map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-right');
        map.addControl(new maplibregl.ScaleControl({ maxWidth: 100, unit: 'metric' }), 'bottom-left');

        let moveTimer: ReturnType<typeof setTimeout> | undefined;
        const onMoveEnd = () => {
            const c = map.getCenter();
            cameraRef.current = { center: [c.lng, c.lat], zoom: map.getZoom() };
            clearTimeout(moveTimer);
            moveTimer = setTimeout(() => setViewport(readViewport(map)), MOVE_DEBOUNCE_MS);
        };

        map.on('load', () => {
            setMapReady(true);
            setViewport(readViewport(map));
        });
        map.on('moveend', onMoveEnd);

        mapRef.current = map;
        return () => {
            clearTimeout(moveTimer);
            map.off('moveend', onMoveEnd);
            // Cleared BEFORE remove(), and the layer cleanups below bail out
            // when mapRef no longer holds THEIR map. On unmount React runs
            // effect cleanups in declaration order, so this one runs first
            // and the layer cleanups then find a map with no style; calling
            // getLayer() on it threw, and the error boundary replaced the
            // page ("Something went wrong") on every navigation away. A
            // basemap switch rebuilds the map the same way, with the new map
            // already in mapRef when the old layer cleanups run — an
            // identity check covers both, where a removed-flag would be
            // reset by the new map before the old cleanups saw it.
            mapRef.current = null;
            map.remove();
            setMapReady(false);
            setHover(null);
        };
    }, [readViewport, styleSpec]);

    // ── Fetch on viewport / filter change ───────────────────────────────────
    useEffect(() => {
        if (!viewport) return;

        const controller = new AbortController();
        setLoading(true);
        setError(null);

        const params = new URLSearchParams({
            bbox: viewport.bbox,
            zoom: String(viewport.zoom),
        });
        if (jurisdiction) params.set('jurisdiction', jurisdiction);
        if (polygonLayers.length) params.set('layers', polygonLayers.join(','));

        fetch(`/api/v1/public-geoscience/map?${params.toString()}`, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            signal: controller.signal,
        })
            .then((res) => {
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                return res.json() as Promise<MapResponse>;
            })
            .then((body) => {
                setData(body);
                setSeenJurisdictions((prev) => {
                    const next = new Set(prev);
                    for (const f of body.features) {
                        if (!f.properties.cluster) next.add(f.properties.jurisdiction_code);
                    }
                    return next.size === prev.length ? prev : Array.from(next).sort();
                });
            })
            .catch((err) => {
                // An aborted in-flight request is the normal result of
                // panning again before the last query returned, not a fault.
                if (err instanceof DOMException && err.name === 'AbortError') return;
                setError(err instanceof Error ? err.message : String(err));
                setData(null);
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });

        return () => controller.abort();
    }, [viewport, jurisdiction, polygonLayers]);

    // ── Polygon overlays (declared before the point layers so they draw under them) ──
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !data) return;

        const geojson = {
            type: 'FeatureCollection' as const,
            features: data.polygons?.features ?? [],
        };

        const existing = map.getSource(POLYGON_SOURCE_ID) as GeoJSONSource | undefined;
        if (existing) {
            existing.setData(geojson);
            return;
        }

        map.addSource(POLYGON_SOURCE_ID, { type: 'geojson', data: geojson });
        const beforeId = map.getLayer(POINT_LAYER_ID) ? POINT_LAYER_ID : undefined;
        map.addLayer(
            {
                id: POLYGON_FILL_LAYER_ID,
                type: 'fill',
                source: POLYGON_SOURCE_ID,
                paint: { 'fill-color': POLYGON_COLOR_MATCH, 'fill-opacity': 0.18 },
            } as unknown as AddLayerObject,
            beforeId,
        );
        map.addLayer(
            {
                id: POLYGON_LINE_LAYER_ID,
                type: 'line',
                source: POLYGON_SOURCE_ID,
                paint: { 'line-color': POLYGON_COLOR_MATCH, 'line-width': 1, 'line-opacity': 0.85 },
            } as unknown as AddLayerObject,
            beforeId,
        );

        return () => {
            if (mapRef.current !== map) return; // the layers went with the map
            for (const id of [POLYGON_LINE_LAYER_ID, POLYGON_FILL_LAYER_ID]) {
                if (map.getLayer(id)) map.removeLayer(id);
            }
            if (map.getSource(POLYGON_SOURCE_ID)) map.removeSource(POLYGON_SOURCE_ID);
        };
    }, [data, mapReady]);

    // ── Source + layers ─────────────────────────────────────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !data) return;

        const geojson = {
            type: 'FeatureCollection' as const,
            features: data.features as unknown as GeoJSON.Feature[],
        };

        const existing = map.getSource(SOURCE_ID) as GeoJSONSource | undefined;
        if (existing) {
            existing.setData(geojson);
            return;
        }

        map.addSource(SOURCE_ID, { type: 'geojson', data: geojson });

        // Individual records.
        map.addLayer({
            id: POINT_LAYER_ID,
            type: 'circle',
            source: SOURCE_ID,
            filter: ['!=', ['get', 'cluster'], true],
            // The Workspace map's collar dot: zoom-scaled, dark keyline.
            paint: {
                'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 3.5, 8, 5, 14, 9, 18, 14],
                'circle-color': LAYER_COLOR_MATCH,
                'circle-stroke-width': 1.5,
                'circle-stroke-color': '#0a0e14',
            },
        } as unknown as AddLayerObject);

        // Ring around the clicked record, so the card and the map agree on
        // which point it describes.
        map.addLayer({
            id: SELECTED_LAYER_ID,
            type: 'circle',
            source: SOURCE_ID,
            filter: selectedFilter(selectionRef.current),
            paint: {
                'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 8, 8, 10, 14, 15, 18, 21],
                'circle-color': 'rgba(0,0,0,0)',
                'circle-stroke-color': '#f9fafb',
                'circle-stroke-width': 2,
            },
        } as unknown as AddLayerObject);

        // Aggregated cells. Radius scales with the count it stands for, so
        // density stays legible instead of collapsing into a uniform blanket.
        map.addLayer({
            id: CLUSTER_LAYER_ID,
            type: 'circle',
            source: SOURCE_ID,
            filter: ['==', ['get', 'cluster'], true],
            paint: {
                'circle-radius': [
                    'interpolate',
                    ['linear'],
                    ['get', 'point_count'],
                    1,
                    8,
                    100,
                    16,
                    1000,
                    24,
                    10000,
                    34,
                ],
                'circle-color': LAYER_COLOR_MATCH,
                'circle-opacity': 0.72,
                'circle-stroke-width': 1.5,
                'circle-stroke-color': '#0b0f14',
            },
        } as unknown as AddLayerObject);

        map.addLayer({
            id: CLUSTER_COUNT_LAYER_ID,
            type: 'symbol',
            source: SOURCE_ID,
            filter: ['==', ['get', 'cluster'], true],
            layout: {
                'text-field': ['number-format', ['get', 'point_count'], { 'max-fraction-digits': 0 }],
                'text-size': 11,
                'text-allow-overlap': true,
            },
            paint: {
                'text-color': '#0b0f14',
                'text-halo-color': '#f9fafb',
                'text-halo-width': 1,
            },
        } as unknown as AddLayerObject);

        return () => {
            if (mapRef.current !== map) return; // the layers went with the map
            for (const id of [CLUSTER_COUNT_LAYER_ID, CLUSTER_LAYER_ID, SELECTED_LAYER_ID, POINT_LAYER_ID]) {
                if (map.getLayer(id)) map.removeLayer(id);
            }
            if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
        };
    }, [data, mapReady]);

    // ── Selected-record ring follows the card ───────────────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !map.getLayer(SELECTED_LAYER_ID)) return;
        map.setFilter(SELECTED_LAYER_ID, selectedFilter(selection));
    }, [selection, mapReady, data]);

    // ── Hover tooltip, point / polygon card, cluster drill-in ───────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady) return;

        const interactive = [POINT_LAYER_ID, CLUSTER_LAYER_ID];
        const markersAt = (point: maplibregl.PointLike) => {
            const present = interactive.filter((id) => map.getLayer(id));
            return present.length ? map.queryRenderedFeatures(point, { layers: present }) : [];
        };

        // The Workspace map's hover: a small DOM tooltip beside the cursor,
        // not a MapLibre popup.
        const onMove = (e: maplibregl.MapMouseEvent) => {
            const features = markersAt(e.point);
            map.getCanvas().style.cursor = features.length ? 'pointer' : '';
            if (!features.length) {
                setHover(null);
                return;
            }
            const props = features[0].properties as PublicGeoFeature['properties'];
            const layerLabel = PUBLIC_GEO_LAYER_LABELS[props.layer] ?? props.layer;
            setHover(
                props.cluster
                    ? {
                          title: `${props.point_count.toLocaleString()} records`,
                          sub: `${layerLabel} · click to zoom in`,
                          x: e.point.x,
                          y: e.point.y,
                      }
                    : {
                          title: props.label ?? layerLabel,
                          sub: `${layerLabel} · ${props.jurisdiction_code} · click for detail`,
                          x: e.point.x,
                          y: e.point.y,
                      },
            );
        };

        const onLeave = () => {
            setHover(null);
            map.getCanvas().style.cursor = '';
        };

        // One click handler, in priority order: a cluster zooms toward its
        // records (the only way to reach them); a point opens its card; a
        // polygon under neither opens the polygon's card; empty map closes it.
        const onClick = (e: maplibregl.MapMouseEvent) => {
            const hit = markersAt(e.point)[0];
            if (hit) {
                const props = hit.properties as PublicGeoFeature['properties'];
                const coords = (hit.geometry as GeoJSON.Point).coordinates as [number, number];
                if (props.cluster) {
                    map.easeTo({ center: coords, zoom: Math.min(map.getZoom() + 2, 18) });
                    return;
                }
                setSelection({
                    kind: 'point',
                    layer: props.layer,
                    id: String(props.id),
                    sourceId: String(props.source_id),
                    label: props.label ?? null,
                    jurisdiction: props.jurisdiction_code,
                    lngLat: coords,
                });
                return;
            }
            const polygons = map.getLayer(POLYGON_FILL_LAYER_ID)
                ? map.queryRenderedFeatures(e.point, { layers: [POLYGON_FILL_LAYER_ID] })
                : [];
            setSelection(
                polygons.length ? { kind: 'polygon', props: polygons[0].properties as PolygonFeatureProperties } : null,
            );
        };

        map.on('mousemove', onMove);
        map.on('mouseout', onLeave);
        map.on('click', onClick);
        return () => {
            map.off('mousemove', onMove);
            map.off('mouseout', onLeave);
            map.off('click', onClick);
        };
    }, [mapReady]);

    const summary = useMemo(() => {
        if (loading && !data) return 'Loading…';
        if (error) return null;
        if (!data) return '0 records in view';

        const total = data.total_in_view;
        const drawn = data.feature_count;
        const base = `${total.toLocaleString()} record${total === 1 ? '' : 's'} in view`;
        if (total === 0) return 'No public geoscience records in this view';
        // Never let the drawn count masquerade as the real one.
        return drawn < total ? `${base} · ${drawn.toLocaleString()} clusters drawn` : base;
    }, [data, error, loading]);

    const clustered = data ? Object.values(data.modes).includes('clustered') : false;

    return (
        <>
            <Head title="Public Geoscience" />
            <div
                className="flex-1 flex flex-col overflow-hidden"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow="PUBLIC GEOSCIENCE"
                    title="Public Geoscience"
                    sub={
                        <span>
                            {error ? <span className="text-red-400">{error}</span> : summary}
                            {
                                ' · mines, mineral occurrences, public drillholes, rock samples, tenure & geology overlays'
                            }
                        </span>
                    }
                />

                <div
                    className="px-8 py-3 flex items-center gap-3 border-b flex-wrap"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    <label
                        htmlFor="pg-jurisdiction"
                        className="text-[10px] font-mono uppercase tracking-wider"
                        style={{ color: 'var(--fg-3)' }}
                    >
                        Jurisdiction
                    </label>
                    <select
                        id="pg-jurisdiction"
                        value={jurisdiction}
                        onChange={(e) => setJurisdiction(e.target.value)}
                        className="text-[11px] font-mono bg-transparent border rounded px-2 py-1"
                        style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)' }}
                    >
                        <option value="">All</option>
                        {seenJurisdictions.map((code) => (
                            <option key={code} value={code}>
                                {code}
                            </option>
                        ))}
                    </select>

                    <div
                        className="flex items-center gap-3 ml-4 text-[10px] font-mono"
                        style={{ color: 'var(--fg-3)' }}
                    >
                        {(Object.keys(PUBLIC_GEO_LAYER_LABELS) as Array<keyof typeof PUBLIC_GEO_LAYER_LABELS>).map(
                            (key) => (
                                <span key={key} className="flex items-center gap-1.5">
                                    <span
                                        className="w-2 h-2 rounded-full inline-block"
                                        style={{ background: PUBLIC_GEO_LAYER_COLORS[key] }}
                                        aria-hidden="true"
                                    />
                                    {PUBLIC_GEO_LAYER_LABELS[key]}
                                </span>
                            ),
                        )}
                    </div>

                    {clustered && (
                        <span className="text-[10px] font-mono ml-auto" style={{ color: 'var(--fg-3)' }}>
                            Clustered — numbers are record counts; zoom in to resolve
                        </span>
                    )}
                    {data?.truncated && (
                        <span className="text-[10px] font-mono text-amber-400">
                            View clipped — zoom in for complete coverage
                        </span>
                    )}
                    {loading && data && (
                        <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>
                            Updating…
                        </span>
                    )}
                </div>

                <div
                    className="px-8 py-2 flex items-center gap-4 border-b flex-wrap"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                        Overlays
                    </span>
                    <PolygonLayerToggles
                        enabled={polygonLayers}
                        onChange={setPolygonLayers}
                        meta={data?.polygon_layers}
                    />
                    <div className="ml-auto">
                        <PublicGeoSyncControls isAdmin={isAdmin} />
                    </div>
                </div>

                {/* The map element is sized with an inline style, not Tailwind:
                    maplibre-gl.css sets `.maplibregl-map { position: relative }`
                    unlayered, which beats Tailwind v4's layered `absolute`
                    utility. On the old `absolute inset-0` element that
                    collapsed the container to zero height, MapLibre fell back
                    to a 400x300 canvas inside it, and the page showed record
                    counts over an empty map. Same pattern as WorkspaceMap. */}
                <div className="flex-1 relative min-h-0">
                    <div className="absolute inset-0">
                        <div ref={mapContainer} style={{ width: '100%', height: '100%' }} />
                    </div>

                    {/* Hover tooltip — the Workspace map's, hidden while a card is open. */}
                    {hover && !selection && (
                        <div
                            className="absolute z-10 pointer-events-none text-[10px] font-mono px-2 py-1 rounded border"
                            style={{
                                left: hover.x + 12,
                                top: hover.y + 12,
                                background: 'var(--bg-1)',
                                borderColor: 'var(--line-2)',
                                color: 'var(--fg-1)',
                            }}
                        >
                            <div style={{ color: 'var(--fg-0)' }}>{hover.title}</div>
                            <div style={{ color: 'var(--fg-3)' }}>{hover.sub}</div>
                        </div>
                    )}

                    {selection && (
                        <PublicGeoFeatureCard
                            selection={selection}
                            sources={data?.sources}
                            onClose={() => setSelection(null)}
                        />
                    )}

                    {/* Basemap picker — same control, same place as the Workspace map's. */}
                    <div
                        className="absolute bottom-8 right-2 z-10 flex items-center gap-2 text-[10px] font-mono px-2 py-1.5 rounded border"
                        style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-2)' }}
                    >
                        <span className="uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                            Map
                        </span>
                        <select
                            aria-label="Basemap"
                            value={basemap}
                            onChange={(e) => setBasemap(e.target.value as BasemapId)}
                            className="text-[10px] font-mono px-1.5 py-0.5 rounded border"
                            style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
                        >
                            {BASEMAP_OPTIONS.map((o) => (
                                <option key={o.id} value={o.id}>
                                    {o.label}
                                </option>
                            ))}
                        </select>
                    </div>
                </div>
            </div>
        </>
    );
}
