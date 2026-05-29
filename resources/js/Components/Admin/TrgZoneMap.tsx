import { useEffect, useRef, useState, type JSX } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';

/**
 * TrgZoneMap — MapLibre panel for Phase H4 §8 TRG cockpit.
 *
 * Fetches GET /admin/target-recommendation/runs/{run_id}/geojson and
 * renders the ranked zones as a choropleth where fill colour scales
 * with aggregate_score. Selected zone (driven by parent state) is
 * outlined.
 */

type Props = {
    runId: string;
    selectedZoneId: string | null;
    onZoneClick?: (zoneId: string) => void;
};

type Feature = GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon, {
    zone_id: string;
    rank: number;
    aggregate_score: number | null;
}>;

type FeatureCollection = GeoJSON.FeatureCollection<
    GeoJSON.Polygon | GeoJSON.MultiPolygon, Feature['properties']
>;

const FALLBACK_STYLE = 'https://demotiles.maplibre.org/style.json';

export function TrgZoneMap({ runId, selectedZoneId, onZoneClick }: Props): JSX.Element {
    const containerRef = useRef<HTMLDivElement | null>(null);
    const mapRef = useRef<maplibregl.Map | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState<boolean>(true);

    useEffect(() => {
        if (!containerRef.current) return;
        const map = new maplibregl.Map({
            container: containerRef.current,
            style: FALLBACK_STYLE,
            center: [-105, 56],
            zoom: 4,
            attributionControl: {},
        });
        mapRef.current = map;

        map.on('load', async () => {
            try {
                const resp = await fetch(
                    `/admin/target-recommendation/runs/${runId}/geojson`,
                    { credentials: 'include' },
                );
                if (!resp.ok) {
                    setError(`Could not load zones: HTTP ${resp.status}`);
                    setLoading(false);
                    return;
                }
                const fc = (await resp.json()) as FeatureCollection;
                if (!fc.features?.length) {
                    setError('No zones to render — the run has no polygons yet.');
                    setLoading(false);
                    return;
                }

                map.addSource('trg-zones', { type: 'geojson', data: fc });
                map.addLayer({
                    id: 'trg-zones-fill',
                    type: 'fill',
                    source: 'trg-zones',
                    paint: {
                        'fill-color': [
                            'case',
                            ['==', ['get', 'zone_id'], selectedZoneId ?? '__none__'],
                            '#fcd34d',
                            [
                                'interpolate', ['linear'], ['coalesce', ['get', 'aggregate_score'], 0],
                                0,   '#cbd5e1',
                                0.3, '#fbbf24',
                                0.6, '#f97316',
                                1.0, '#dc2626',
                            ],
                        ],
                        'fill-opacity': 0.55,
                    },
                });
                map.addLayer({
                    id: 'trg-zones-outline',
                    type: 'line',
                    source: 'trg-zones',
                    paint: {
                        'line-color': [
                            'case',
                            ['==', ['get', 'zone_id'], selectedZoneId ?? '__none__'],
                            '#f59e0b',
                            '#475569',
                        ],
                        'line-width': [
                            'case',
                            ['==', ['get', 'zone_id'], selectedZoneId ?? '__none__'],
                            3,
                            1,
                        ],
                    },
                });
                map.addLayer({
                    id: 'trg-zones-rank',
                    type: 'symbol',
                    source: 'trg-zones',
                    layout: {
                        'text-field': ['concat', '#', ['to-string', ['get', 'rank']]],
                        'text-font': ['Open Sans Bold'],
                        'text-size': 12,
                    },
                    paint: {
                        'text-color': '#1f2937',
                        'text-halo-color': '#ffffff',
                        'text-halo-width': 1.5,
                    },
                });

                // Fit to bounds.
                const bounds = new maplibregl.LngLatBounds();
                fc.features.forEach(f => {
                    const expand = (coords: number[][]) => {
                        coords.forEach(c => {
                            if (typeof c[0] === 'number' && typeof c[1] === 'number') {
                                bounds.extend([c[0], c[1]]);
                            }
                        });
                    };
                    if (f.geometry.type === 'Polygon') {
                        f.geometry.coordinates.forEach(ring => expand(ring as number[][]));
                    } else if (f.geometry.type === 'MultiPolygon') {
                        f.geometry.coordinates.forEach(poly => poly.forEach(ring => expand(ring as number[][])));
                    }
                });
                if (!bounds.isEmpty()) {
                    map.fitBounds(bounds, { padding: 40, duration: 0 });
                }

                map.on('click', 'trg-zones-fill', (e) => {
                    const f = e.features?.[0];
                    if (f && onZoneClick) onZoneClick((f.properties as { zone_id: string }).zone_id);
                });
                map.on('mouseenter', 'trg-zones-fill', () => {
                    map.getCanvas().style.cursor = 'pointer';
                });
                map.on('mouseleave', 'trg-zones-fill', () => {
                    map.getCanvas().style.cursor = '';
                });
                setLoading(false);
            } catch (err) {
                setError(`Map load error: ${(err as Error).message}`);
                setLoading(false);
            }
        });

        return () => {
            map.remove();
            mapRef.current = null;
        };
    }, [runId]);

    // When selectedZoneId changes, update paint properties in place.
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.isStyleLoaded()) return;
        try {
            map.setPaintProperty('trg-zones-fill', 'fill-color', [
                'case',
                ['==', ['get', 'zone_id'], selectedZoneId ?? '__none__'],
                '#fcd34d',
                [
                    'interpolate', ['linear'], ['coalesce', ['get', 'aggregate_score'], 0],
                    0,   '#cbd5e1',
                    0.3, '#fbbf24',
                    0.6, '#f97316',
                    1.0, '#dc2626',
                ],
            ]);
            map.setPaintProperty('trg-zones-outline', 'line-color', [
                'case',
                ['==', ['get', 'zone_id'], selectedZoneId ?? '__none__'],
                '#f59e0b',
                '#475569',
            ]);
            map.setPaintProperty('trg-zones-outline', 'line-width', [
                'case',
                ['==', ['get', 'zone_id'], selectedZoneId ?? '__none__'],
                3,
                1,
            ]);
        } catch {
            // layer may not exist yet
        }
    }, [selectedZoneId]);

    return (
        <div className="relative w-full h-[60vh] border rounded overflow-hidden">
            <div ref={containerRef} className="absolute inset-0" />
            {loading && (
                <div className="absolute inset-0 flex items-center justify-center bg-white/60 text-sm text-gray-600">
                    Loading zones…
                </div>
            )}
            {error && (
                <div className="absolute top-2 left-2 p-2 bg-amber-50 border border-amber-200 text-amber-800 text-xs rounded">
                    {error}
                </div>
            )}
        </div>
    );
}

export default TrgZoneMap;
