import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Head } from '@inertiajs/react';
import maplibregl from 'maplibre-gl';
import type { Map as MapLibreMap, Popup, GeoJSONSource, AddLayerObject } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader } from '@/Components/Foundry/primitives';
import { useBasemapStyleUrl } from '@/lib/basemap';
import { escapeHtml } from '@/lib/escapeHtml';
import {
    PUBLIC_GEO_LAYER_LABELS,
    PUBLIC_GEO_LAYER_COLORS,
    type PublicGeoFeature,
    type PublicGeoFeatureCollection,
} from '@/Components/MapView';

const SOURCE_ID = 'public-geoscience';
const POINT_LAYER_ID = 'public-geoscience-points';
const CLUSTER_LAYER_ID = 'public-geoscience-clusters';
const CLUSTER_COUNT_LAYER_ID = 'public-geoscience-cluster-counts';

/** How long the map must sit still before we re-query. */
const MOVE_DEBOUNCE_MS = 350;

const LAYER_COLOR_MATCH = [
    'match',
    ['get', 'layer'],
    'mine', PUBLIC_GEO_LAYER_COLORS.mine,
    'mineral_occurrence', PUBLIC_GEO_LAYER_COLORS.mineral_occurrence,
    'drillhole_collar', PUBLIC_GEO_LAYER_COLORS.drillhole_collar,
    'rock_sample', PUBLIC_GEO_LAYER_COLORS.rock_sample,
    '#9ca3af',
];

interface Viewport {
    bbox: string;
    zoom: number;
}

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
 * See PublicGeoscienceMapController for the data scope (4 point-geometry
 * public_geo tables; polygons excluded) and for the empty-in-production
 * caveat — as of 2026-08-19 Azure has the schema but none of the rows.
 */
export default function PublicGeoscience() {
    const mapContainer = useRef<HTMLDivElement | null>(null);
    const mapRef = useRef<MapLibreMap | null>(null);
    const popupRef = useRef<Popup | null>(null);
    const [mapReady, setMapReady] = useState(false);
    const [data, setData] = useState<PublicGeoFeatureCollection | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [jurisdiction, setJurisdiction] = useState('');
    const [viewport, setViewport] = useState<Viewport | null>(null);

    const styleUrl = useBasemapStyleUrl('positron');

    // Jurisdiction codes accumulate across fetches instead of being derived
    // from the current response. Deriving them per-response would make the
    // dropdown's options change as you pan — and selecting a jurisdiction
    // narrows the response, which would then drop every other option out of
    // the list you just used.
    const [seenJurisdictions, setSeenJurisdictions] = useState<string[]>([]);

    const readViewport = useCallback((map: MapLibreMap): Viewport => {
        const b = map.getBounds();
        return {
            bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
                .map((n) => n.toFixed(5))
                .join(','),
            zoom: Math.round(map.getZoom() * 10) / 10,
        };
    }, []);

    // ── Init map once ───────────────────────────────────────────────────────
    useEffect(() => {
        if (!mapContainer.current) return;

        const map = new maplibregl.Map({
            container: mapContainer.current,
            style: styleUrl,
            center: [-107, 55],
            zoom: 4,
        });

        map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-right');
        map.addControl(new maplibregl.ScaleControl({ maxWidth: 100, unit: 'metric' }), 'bottom-left');

        let moveTimer: ReturnType<typeof setTimeout> | undefined;
        const onMoveEnd = () => {
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
            map.remove();
            mapRef.current = null;
            setMapReady(false);
        };
    }, [readViewport]);

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

        fetch(`/api/v1/public-geoscience/map?${params.toString()}`, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            signal: controller.signal,
        })
            .then((res) => {
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                return res.json() as Promise<PublicGeoFeatureCollection>;
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
    }, [viewport, jurisdiction]);

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
            paint: {
                'circle-radius': 5,
                'circle-color': LAYER_COLOR_MATCH,
                'circle-stroke-width': 1,
                'circle-stroke-color': '#0b0f14',
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
                    'interpolate', ['linear'], ['get', 'point_count'],
                    1, 8,
                    100, 16,
                    1000, 24,
                    10000, 34,
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
            for (const id of [CLUSTER_COUNT_LAYER_ID, CLUSTER_LAYER_ID, POINT_LAYER_ID]) {
                if (map.getLayer(id)) map.removeLayer(id);
            }
            if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
        };
    }, [data, mapReady]);

    // ── Hover popup + cluster drill-in ──────────────────────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady) return;

        const interactive = [POINT_LAYER_ID, CLUSTER_LAYER_ID];

        const onMove = (e: maplibregl.MapMouseEvent) => {
            const present = interactive.filter((id) => map.getLayer(id));
            if (!present.length) return;
            const features = map.queryRenderedFeatures(e.point, { layers: present });
            map.getCanvas().style.cursor = features.length ? 'pointer' : '';
            if (!features.length) {
                popupRef.current?.remove();
                popupRef.current = null;
                return;
            }
            const feat = features[0];
            const props = feat.properties as PublicGeoFeature['properties'];
            const layerLabel = PUBLIC_GEO_LAYER_LABELS[props.layer] ?? props.layer;

            const html = props.cluster
                ? `<div style="font: 11px monospace; color: #e5e7eb;">
                        <div style="font-weight: 600;">${props.point_count.toLocaleString()} records</div>
                        <div style="color: #9ca3af;">${escapeHtml(layerLabel)} · click to zoom in</div>
                   </div>`
                : `<div style="font: 11px monospace; color: #e5e7eb;">
                        <div style="font-weight: 600;">${escapeHtml(props.label ?? layerLabel)}</div>
                        <div style="color: #9ca3af;">${escapeHtml(layerLabel)} · ${escapeHtml(props.jurisdiction_code)}</div>
                   </div>`;

            popupRef.current?.remove();
            popupRef.current = new maplibregl.Popup({ closeButton: false, closeOnClick: false })
                .setLngLat((feat.geometry as GeoJSON.Point).coordinates as [number, number])
                .setHTML(html)
                .addTo(map);
        };

        const onLeave = () => {
            popupRef.current?.remove();
            popupRef.current = null;
            map.getCanvas().style.cursor = '';
        };

        // Clicking a cluster is the only way to reach the records inside it,
        // so it has to actually resolve — zoom two levels toward the cell.
        const onClusterClick = (e: maplibregl.MapMouseEvent) => {
            if (!map.getLayer(CLUSTER_LAYER_ID)) return;
            const hits = map.queryRenderedFeatures(e.point, { layers: [CLUSTER_LAYER_ID] });
            if (!hits.length) return;
            const coords = (hits[0].geometry as GeoJSON.Point).coordinates as [number, number];
            map.easeTo({ center: coords, zoom: Math.min(map.getZoom() + 2, 18) });
        };

        map.on('mousemove', onMove);
        map.on('mouseout', onLeave);
        map.on('click', onClusterClick);
        return () => {
            map.off('mousemove', onMove);
            map.off('mouseout', onLeave);
            map.off('click', onClusterClick);
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
        <AppLayout>
            <Head title="Public Geoscience" />
            <div className="flex-1 flex flex-col overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="PUBLIC GEOSCIENCE"
                    title="Public Geoscience"
                    sub={
                        <span>
                            {error ? <span className="text-red-400">{error}</span> : summary}
                            {' · mines, mineral occurrences, public drillholes, rock samples'}
                        </span>
                    }
                />

                <div className="px-8 py-3 flex items-center gap-3 border-b flex-wrap" style={{ borderColor: 'var(--line-1)' }}>
                    <label htmlFor="pg-jurisdiction" className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
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
                            <option key={code} value={code}>{code}</option>
                        ))}
                    </select>

                    <div className="flex items-center gap-3 ml-4 text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>
                        {(Object.keys(PUBLIC_GEO_LAYER_LABELS) as Array<keyof typeof PUBLIC_GEO_LAYER_LABELS>).map((key) => (
                            <span key={key} className="flex items-center gap-1.5">
                                <span
                                    className="w-2 h-2 rounded-full inline-block"
                                    style={{ background: PUBLIC_GEO_LAYER_COLORS[key] }}
                                    aria-hidden="true"
                                />
                                {PUBLIC_GEO_LAYER_LABELS[key]}
                            </span>
                        ))}
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
                        <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>Updating…</span>
                    )}
                </div>

                <div className="flex-1 relative">
                    <div ref={mapContainer} className="absolute inset-0" />
                </div>
            </div>
        </AppLayout>
    );
}
