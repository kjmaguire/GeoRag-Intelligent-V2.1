import { useEffect, useMemo, useRef, useState } from 'react';
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
const LAYER_ID = 'public-geoscience-points';

/**
 * Foundry/PublicGeoscience — standalone browse page for /public-geoscience,
 * linked from the top ORG nav bar.
 *
 * 2026-08-17 — this is a from-scratch page, not a restore of the old
 * PublicGeoscienceMap.tsx (Martin/MVT-backed, deleted with the tile server
 * itself). It's a plain MapLibre GeoJSON source/layer against
 * GET /api/v1/public-geoscience/map — same pattern as MapView.tsx's
 * coverage-density and public-geoscience-overlay layers, just not nested
 * inside a per-project map. See PublicGeoscienceMapController's docblock
 * for data scope (4 point-geometry public_geo tables; polygons excluded).
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

    const styleUrl = useBasemapStyleUrl('positron');

    const jurisdictions = useMemo(() => {
        if (!data) return [];
        const codes = new Set(data.features.map((f) => f.properties.jurisdiction_code));
        return Array.from(codes).sort();
    }, [data]);

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
        map.on('load', () => setMapReady(true));

        mapRef.current = map;
        return () => {
            map.remove();
            mapRef.current = null;
            setMapReady(false);
        };
    }, []);

    // ── Fetch data (re-fetch on jurisdiction filter change) ────────────────
    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError(null);

        const qs = jurisdiction ? `?jurisdiction=${encodeURIComponent(jurisdiction)}` : '';
        fetch(`/api/v1/public-geoscience/map${qs}`, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then((res) => {
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                return res.json() as Promise<PublicGeoFeatureCollection>;
            })
            .then((body) => {
                if (!cancelled) setData(body);
            })
            .catch((err) => {
                if (!cancelled) {
                    setError(err instanceof Error ? err.message : String(err));
                    setData(null);
                }
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });

        return () => {
            cancelled = true;
        };
    }, [jurisdiction]);

    // ── Source/layer + hover popup ──────────────────────────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !data) return;

        const geojson = { type: 'FeatureCollection' as const, features: data.features as unknown as GeoJSON.Feature[] };
        const existing = map.getSource(SOURCE_ID) as GeoJSONSource | undefined;
        if (existing) {
            existing.setData(geojson);
        } else {
            map.addSource(SOURCE_ID, { type: 'geojson', data: geojson });
            map.addLayer({
                id: LAYER_ID,
                type: 'circle',
                source: SOURCE_ID,
                paint: {
                    'circle-radius': 5,
                    'circle-color': [
                        'match',
                        ['get', 'layer'],
                        'mine', PUBLIC_GEO_LAYER_COLORS.mine,
                        'mineral_occurrence', PUBLIC_GEO_LAYER_COLORS.mineral_occurrence,
                        'drillhole_collar', PUBLIC_GEO_LAYER_COLORS.drillhole_collar,
                        'rock_sample', PUBLIC_GEO_LAYER_COLORS.rock_sample,
                        '#9ca3af',
                    ],
                    'circle-stroke-width': 1,
                    'circle-stroke-color': '#0b0f14',
                },
            } as unknown as AddLayerObject);
        }

        return () => {
            if (map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID);
            if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
        };
    }, [data, mapReady]);

    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady) return;

        const onMove = (e: maplibregl.MapMouseEvent) => {
            const features = map.queryRenderedFeatures(e.point, { layers: [LAYER_ID] });
            map.getCanvas().style.cursor = features.length ? 'pointer' : '';
            if (!features.length) {
                popupRef.current?.remove();
                popupRef.current = null;
                return;
            }
            const feat = features[0];
            const props = feat.properties as PublicGeoFeature['properties'];
            const layerLabel = PUBLIC_GEO_LAYER_LABELS[props.layer] ?? props.layer;
            const html = `
                <div style="font: 11px monospace; color: #e5e7eb;">
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

        map.on('mousemove', LAYER_ID, onMove);
        map.on('mouseleave', LAYER_ID, onLeave);
        return () => {
            map.off('mousemove', LAYER_ID, onMove);
            map.off('mouseleave', LAYER_ID, onLeave);
        };
    }, [mapReady]);

    return (
        <AppLayout>
            <Head title="Public Geoscience" />
            <div className="flex-1 flex flex-col overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="PUBLIC GEOSCIENCE"
                    title="Public Geoscience"
                    sub={
                        <span>
                            {loading ? 'Loading…' : error ? <span className="text-red-400">{error}</span> : `${data?.feature_count ?? 0} features`}
                            {' · mines, mineral occurrences, public drillholes, rock samples'}
                        </span>
                    }
                />

                <div className="px-8 py-3 flex items-center gap-3 border-b" style={{ borderColor: 'var(--line-1)' }}>
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
                        {jurisdictions.map((code) => (
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
                </div>

                <div className="flex-1 relative">
                    <div ref={mapContainer} className="absolute inset-0" />
                </div>
            </div>
        </AppLayout>
    );
}
