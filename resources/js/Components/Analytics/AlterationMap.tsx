import { useEffect, useMemo, useRef } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { useBasemapStyleUrl } from '@/lib/basemap';

interface Collar {
    collar_id: string;
    hole_id: string;
    longitude: number | null;
    latitude: number | null;
}

interface GeochemRow {
    collar_id: string;
    cia: number | null;
}

interface Props {
    collars: Collar[];
    geochem: GeochemRow[];
}

/**
 * Map of drill collars coloured by their mean Chemical Index of
 * Alteration (CIA). Hotter colours = more alteration (higher CIA).
 *
 * CIA is Al₂O₃ / (Al₂O₃ + CaO + Na₂O + K₂O) × 100. Protolith CIA sits
 * around 50; basement pelites weather to 70–90. Plotting mean CIA per
 * collar surfaces spatial trends — are alteration hot-spots clustered?
 *
 * Rendering approach: MapLibre base map (OpenFreeMap positron) plus a
 * circle layer backed by an inline GeoJSON source. Circle radius is
 * proportional to sample count so you can see how reliable the
 * per-collar mean is visually. No raster heatmap — with ~20 collars the
 * point-symbol approach reads more cleanly.
 */
// V1.5-09 — feature shape for the inline GeoJSON source. Narrow Point so
// MapLibre's `addSource({ type: 'geojson', data })` accepts the FeatureCollection
// without `as unknown as` casts. CIA properties live in `properties`.
type CiaFeature = GeoJSON.Feature<GeoJSON.Point, {
    hole_id: string;
    cia_mean: number | null;
    sample_count: number;
}>;

export default function AlterationMap({ collars, geochem }: Props) {
    // V1.5-09 — typed refs so getSource / fitBounds / resize compile clean.
    // The previous untyped useRef(null) inferred `never`, breaking every
    // method call inside the load + bbox effects.
    const mapRef = useRef<maplibregl.Map | null>(null);
    const containerRef = useRef<HTMLDivElement | null>(null);
    // Config-driven basemap URL — analytics uses the dark-matter style.
    const darkMatterStyleUrl = useBasemapStyleUrl('dark_matter');

    const features = useMemo<CiaFeature[]>(() => {
        // Bucket CIA rows by collar, compute mean + sample count.
        const buckets: Record<string, number[]> = {};
        for (const r of geochem) {
            if (r.cia == null) continue;
            (buckets[r.collar_id] = buckets[r.collar_id] || []).push(r.cia);
        }

        const featureList: CiaFeature[] = [];
        for (const c of collars) {
            if (c.longitude == null || c.latitude == null) continue;
            const vals = buckets[c.collar_id] || [];
            const mean = vals.length > 0
                ? vals.reduce((s, v) => s + v, 0) / vals.length
                : null;
            featureList.push({
                type: 'Feature',
                geometry: { type: 'Point', coordinates: [c.longitude, c.latitude] },
                properties: {
                    hole_id: c.hole_id,
                    cia_mean: mean,
                    sample_count: vals.length,
                },
            });
        }
        return featureList;
    }, [collars, geochem]);

    // V1.5-09 — typed as the SW/NE corner pair MapLibre's LngLatBoundsLike
    // accepts ([sw, ne] of [lng, lat]). Returning null short-circuits the
    // fitBounds path when no points are renderable.
    const bbox = useMemo<[[number, number], [number, number]] | null>(() => {
        let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
        for (const c of collars) {
            if (c.longitude == null || c.latitude == null) continue;
            if (c.longitude < minLon) minLon = c.longitude;
            if (c.longitude > maxLon) maxLon = c.longitude;
            if (c.latitude < minLat) minLat = c.latitude;
            if (c.latitude > maxLat) maxLat = c.latitude;
        }
        if (!Number.isFinite(minLon)) return null;
        const padLon = Math.max(0.001, (maxLon - minLon) * 0.2);
        const padLat = Math.max(0.001, (maxLat - minLat) * 0.2);
        return [[minLon - padLon, minLat - padLat], [maxLon + padLon, maxLat + padLat]];
    }, [collars]);

    useEffect(() => {
        if (!containerRef.current) return;
        if (mapRef.current) return;

        // CartoDB `dark-matter` — a near-black vector-tile style designed
        // for dark analytics UIs. Labels render light so they remain
        // readable, and the alteration-circle colour ramp (blue→red)
        // pops cleanly against the dark base.
        //
        // When we move to full on-prem deployment the on-prem hardening
        // milestone swaps this for a dark style served through our own
        // Martin tile host (same pattern MapView already uses for the
        // default map). Tracked with the rest of the air-gap checklist.
        const map = new maplibregl.Map({
            container: containerRef.current,
            style: darkMatterStyleUrl,
            center: bbox ? [
                (bbox[0][0] + bbox[1][0]) / 2,
                (bbox[0][1] + bbox[1][1]) / 2,
            ] : [-106, 57],
            zoom: 8,
        });
        mapRef.current = map;
        // Temporary debug export — lets us poke at the map state from
        // the console while diagnosing the blank-canvas bug.
        if (typeof window !== 'undefined') (window as any).__altmap = map;

        map.on('load', () => {
            // eslint-disable-next-line no-console
            console.log('[AlterationMap] load fired, features=', features.length);
            map.addSource('collars-cia', {
                type: 'geojson',
                data: { type: 'FeatureCollection', features },
            });

            // Colour ramp — CIA 50 (protolith) → 90 (heavily altered).
            map.addLayer({
                id: 'cia-circles',
                type: 'circle',
                source: 'collars-cia',
                paint: {
                    // Minimum radius 7 so individual collars never
                    // shrink below readable at typical zoom levels.
                    'circle-radius': [
                        'interpolate', ['linear'], ['get', 'sample_count'],
                        0, 7,
                        5, 11,
                        20, 18,
                    ],
                    'circle-color': [
                        'case',
                        ['==', ['get', 'cia_mean'], null],
                        '#64748b',
                        [
                            'interpolate', ['linear'], ['get', 'cia_mean'],
                            50, '#3b82f6',   // blue = fresh
                            60, '#22c55e',
                            70, '#eab308',
                            80, '#f97316',
                            90, '#ef4444',   // red = altered
                        ],
                    ],
                    'circle-stroke-width': 1.5,
                    'circle-stroke-color': 'rgba(15,23,42,0.9)',
                    'circle-opacity': 0.9,
                },
            });

            map.addLayer({
                id: 'cia-labels',
                type: 'symbol',
                source: 'collars-cia',
                layout: {
                    'text-field': ['get', 'hole_id'],
                    'text-size': 10,
                    'text-offset': [0, 1.4],
                    'text-anchor': 'top',
                },
                paint: {
                    // Light text + dark halo for the dark-matter basemap.
                    'text-color': '#f8fafc',
                    'text-halo-color': 'rgba(15,23,42,0.85)',
                    'text-halo-width': 1.4,
                },
            });

            if (bbox) {
                // maxZoom 12 keeps the whole drill grid in view even when
                // the collars span only a few km. Previous 14 was zooming
                // past the grid and stacking 3-4 holes in one corner.
                map.fitBounds(bbox, { padding: 48, duration: 0, maxZoom: 12 });
            }

            // Hover popup
            const popup = new maplibregl.Popup({
                closeButton: false,
                closeOnClick: false,
                offset: 12,
            });

            map.on('mouseenter', 'cia-circles', (e) => {
                map.getCanvas().style.cursor = 'pointer';
                const f = e.features?.[0];
                if (!f) return;
                const props = f.properties as any;
                const cia = props.cia_mean != null ? Number(props.cia_mean).toFixed(1) : '—';
                popup
                    .setLngLat((f.geometry as any).coordinates)
                    .setHTML(
                        `<div style="font-family:ui-sans-serif;font-size:11px;color:#111">
                            <div style="font-weight:600">${props.hole_id}</div>
                            <div>Mean CIA: <b>${cia}</b></div>
                            <div style="color:#64748b">${props.sample_count} samples</div>
                         </div>`,
                    )
                    .addTo(map);
            });
            map.on('mouseleave', 'cia-circles', () => {
                map.getCanvas().style.cursor = '';
                popup.remove();
            });
        });

        return () => {
            try { map.remove(); } catch { /* noop */ }
            mapRef.current = null;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Update source data when features change without re-creating the map.
    useEffect(() => {
        const map = mapRef.current;
        if (!map) return;
        // V1.5-09 — narrow the Source union to GeoJSONSource so setData()
        // type-resolves cleanly. The instanceof check is the canonical
        // narrow per maplibre-gl typings.
        const src = map.getSource('collars-cia');
        if (src instanceof maplibregl.GeoJSONSource) {
            src.setData({ type: 'FeatureCollection', features });
            if (bbox) map.fitBounds(bbox, { padding: 48, duration: 300, maxZoom: 12 });
        }
    }, [features, bbox]);

    // ResizeObserver on the container — MapLibre initialises using the
    // container size at the moment the effect runs. If the parent's
    // height isn't settled yet (e.g. lazy-loaded panel mounting inside a
    // Suspense tree), the map ends up with a 0-height internal viewport
    // and renders nothing even though the canvas fills the card later.
    // We watch the container and call map.resize() + refit whenever its
    // size changes so the map recovers from any initial layout thrash.
    useEffect(() => {
        const el = containerRef.current;
        const map = mapRef.current;
        if (!el || !map || typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(() => {
            try {
                map.resize();
                if (bbox) map.fitBounds(bbox, { padding: 48, duration: 0, maxZoom: 12 });
            } catch {
                /* map might be mid-teardown — swallow */
            }
        });
        ro.observe(el);
        return () => ro.disconnect();
    }, [bbox]);

    return (
        <div
            className="relative rounded overflow-hidden border border-gray-800"
            style={{ height: 420 }}
        >
            {/* CartoDB dark-matter basemap; collar circles render a
                bright CIA colour ramp on top for high contrast.
                Explicit pixel heights throughout because Tailwind's
                arbitrary-height class wasn't propagating through the
                absolute-positioned MapLibre container on first layout,
                leaving `.maplibregl-map` at clientHeight=0 and the map
                invisible to the user despite the canvas itself painting
                correctly. */}
            <div ref={containerRef} style={{ position: 'absolute', inset: 0, height: '100%', width: '100%' }} />
            <div className="absolute top-2 left-2 bg-gray-900/80 rounded px-2 py-1.5 text-[10px] text-gray-300 font-mono">
                <div className="text-gray-500 mb-1">Mean CIA per collar</div>
                <div className="flex items-center gap-1">
                    <span className="inline-block w-3 h-3 rounded-full" style={{ background: '#3b82f6' }} />
                    <span>50 fresh</span>
                    <span className="inline-block w-3 h-3 rounded-full ml-2" style={{ background: '#22c55e' }} />
                    <span>60</span>
                    <span className="inline-block w-3 h-3 rounded-full ml-2" style={{ background: '#eab308' }} />
                    <span>70</span>
                    <span className="inline-block w-3 h-3 rounded-full ml-2" style={{ background: '#f97316' }} />
                    <span>80</span>
                    <span className="inline-block w-3 h-3 rounded-full ml-2" style={{ background: '#ef4444' }} />
                    <span>90 altered</span>
                </div>
                <div className="text-gray-500 mt-1">Circle size ∝ sample count</div>
            </div>
        </div>
    );
}
