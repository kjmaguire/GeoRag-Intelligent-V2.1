import { useCallback, useEffect, useRef, useState } from 'react';
import type { LayerId } from '@/Components/PublicGeoscience/publicGeoscienceLayers';

/**
 * Full upstream record for a single feature. Shape is per-layer — every
 * canonical PG table has its own column set — so the consumer treats the
 * payload as a `Record<string, unknown>` and renders whichever fields
 * are present.
 *
 * Always present:
 *   - layer                : the LayerId echoed back from the API for
 *                            cross-layer consumers (compare modal)
 *   - source_attributes    : raw upstream JSONB (parsed) or null
 *   - reserves_resources   : raw upstream JSONB (parsed) or null
 *
 * Geometry-related columns (geom, source_geom_wkt, checksum) are
 * stripped server-side because they bloat the response and are never
 * read by the UI.
 */
export interface FeatureDetail {
    layer: LayerId;
    source_attributes?: Record<string, unknown> | null;
    reserves_resources?: Record<string, unknown> | null;
    [key: string]: unknown;
}

interface UseFeatureDetailResult {
    data: FeatureDetail | null;
    loading: boolean;
    error: string | null;
    retry: () => void;
}

/**
 * Fetches the full upstream record for a single MVT feature. Backs the
 * Expanded-Feature panel + the Compare-Features modal — the MVT tile
 * itself only carries a trimmed property set to keep tiles cheap.
 *
 * Endpoint: GET /api/v1/public-geoscience/features/{layer}/{feature_id}
 *           (auth:sanctum)
 *
 * Conditional fetch: passing `null` for either arg returns idle state
 * (no request fires). Re-fetches when the (layer, featureId) tuple
 * changes. In-flight requests for stale targets are aborted via
 * AbortController so a fast click sequence doesn't show the wrong
 * feature in the panel.
 */
export function useFeatureDetail(
    layer: LayerId | null,
    featureId: string | null,
): UseFeatureDetailResult {
    const [data, setData] = useState<FeatureDetail | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const controllerRef = useRef<AbortController | null>(null);

    const fetchData = useCallback(async () => {
        controllerRef.current?.abort();

        if (!layer || !featureId) {
            setData(null);
            setLoading(false);
            setError(null);
            return;
        }

        const controller = new AbortController();
        controllerRef.current = controller;

        setLoading(true);
        setError(null);
        setData(null);

        try {
            const url = `/api/v1/public-geoscience/features/${encodeURIComponent(layer)}/${encodeURIComponent(featureId)}`;
            const response = await fetch(url, {
                signal: controller.signal,
                credentials: 'same-origin',
                headers: {
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const envelope = await response.json();
            // Endpoint returns { data: {...}, generated_at, cache_ttl_seconds }
            setData(envelope.data as FeatureDetail);
        } catch (err) {
            if (err instanceof DOMException && err.name === 'AbortError') return;
            setError(err instanceof Error ? err.message : 'Fetch failed');
        } finally {
            if (!controller.signal.aborted) {
                setLoading(false);
            }
        }
    }, [layer, featureId]);

    useEffect(() => {
        fetchData();
        return () => controllerRef.current?.abort();
    }, [fetchData]);

    return { data, loading, error, retry: fetchData };
}
