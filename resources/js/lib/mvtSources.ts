/**
 * Attaching the silver MVT layers to a MapLibre map.
 *
 * The layer definitions in `mvtLayers.ts` and the URL builder in `tileUrl.ts`
 * have existed for months, and `MapView.tsx` carries a full MVT
 * implementation — but that path never fires: `useMvt` requires
 * `!inlineGeoJson && !!projectId`, and MapView's only production call site
 * passes `inlineGeoJson`. The real geologist map is `WorkspaceMap.tsx`, which
 * had no MVT code at all.
 *
 * This module is the missing join: given a map and a project, it adds the
 * vector sources and the style layers. It lives apart from both components so
 * neither has to own tile plumbing, and so the source-sharing rule below has
 * exactly one implementation.
 *
 * ## Source sharing is not an optimisation
 *
 * `silver.pg_spatial_features_by_project` emits THREE ST_AsMVT layers in one
 * tile — `imported_points` / `imported_lines` / `imported_polygons` — because
 * one MapLibre layer has one `type` and cannot paint points, lines and
 * polygons together. The three defs therefore declare the same `sourceKey`.
 * Adding a source per DEF would open three identical vector sources and fetch
 * the same bytes three times, and MapLibre would warn on the duplicate ids.
 * `mvtSourceId()` in mvtLayers.ts is the shared rule; this module must go
 * through it rather than deriving ids of its own.
 */
import {
    MVT_LAYERS,
    mvtSourceId,
    type MvtLayerDef,
} from '@/lib/mvtLayers';
import { buildSilverTileUrl } from '@/lib/tileUrl';

/** The minimum MapLibre surface this module needs. Keeps maplibre-gl out of
 *  the import graph for tests, and documents exactly what is touched. */
export interface MvtCapableMap {
    getSource(id: string): unknown;
    addSource(id: string, source: Record<string, unknown>): void;
    getLayer(id: string): unknown;
    addLayer(layer: Record<string, unknown>, before?: string): void;
    removeLayer(id: string): void;
    removeSource(id: string): void;
    setLayoutProperty(layer: string, name: string, value: unknown): void;
}

/** MapLibre layer id for a def, and for its optional outline companion. */
export function mvtLayerId(def: MvtLayerDef): string {
    return `mvt-${def.id}`;
}

export function mvtOutlineLayerId(def: MvtLayerDef): string {
    return `mvt-${def.id}-outline`;
}

export interface AddMvtOptions {
    projectId: string;
    /** Workspace data_version; a change re-keys the tile URL and drops the
     *  client cache. 0 when unknown — the server ETag is authoritative. */
    dataVersion: number;
    /** Keyed by def id. A layer absent from the map is treated as hidden. */
    visibleLayers?: Record<string, boolean>;
    /** Insert beneath this layer id, so tiles do not cover labels/markers. */
    beforeId?: string;
    layers?: MvtLayerDef[];
}

/**
 * Add every MVT source and style layer to `map`.
 *
 * Idempotent: an existing source or layer is left alone rather than
 * re-added, because `map.on('load')` and a style change can both land here
 * and MapLibre throws on a duplicate id.
 *
 * @returns the layer ids added, for the caller's teardown.
 */
export function addMvtLayers(map: MvtCapableMap, opts: AddMvtOptions): string[] {
    const defs = opts.layers ?? MVT_LAYERS;
    const added: string[] = [];

    // Sources first, deduplicated by sourceKey. `minzoom`/`maxzoom` are
    // properties of the SOURCE, so shared entries must agree on them; the
    // widest window wins, and a per-layer window is applied on the style
    // layer instead, where it belongs.
    const bySource = new Map<string, MvtLayerDef[]>();
    for (const def of defs) {
        const sid = mvtSourceId(def);
        const group = bySource.get(sid);
        if (group) {
            group.push(def);
        } else {
            bySource.set(sid, [def]);
        }
    }

    for (const [sourceId, group] of bySource) {
        if (map.getSource(sourceId)) {
            continue;
        }
        map.addSource(sourceId, {
            type: 'vector',
            tiles: [buildSilverTileUrl(group[0].functionName, opts.projectId, opts.dataVersion)],
            minzoom: Math.min(...group.map((d) => d.minzoom)),
            maxzoom: Math.max(...group.map((d) => d.maxzoom)),
        });
    }

    for (const def of defs) {
        const sourceId = mvtSourceId(def);
        const layerId = mvtLayerId(def);
        const visible = opts.visibleLayers?.[def.id] ?? false;

        if (!map.getLayer(layerId)) {
            map.addLayer(
                {
                    id: layerId,
                    type: def.type,
                    source: sourceId,
                    'source-layer': def.sourceLayer,
                    minzoom: def.minzoom,
                    maxzoom: def.maxzoom,
                    paint: def.paint,
                    layout: { visibility: visible ? 'visible' : 'none' },
                },
                opts.beforeId,
            );
            added.push(layerId);
        }

        // A fill with no stroke reads as a wash at low zoom; the outline is
        // what makes an imported polygon legible against the basemap.
        if (def.outline && !map.getLayer(mvtOutlineLayerId(def))) {
            map.addLayer(
                {
                    id: mvtOutlineLayerId(def),
                    type: 'line',
                    source: sourceId,
                    'source-layer': def.sourceLayer,
                    minzoom: def.minzoom,
                    maxzoom: def.maxzoom,
                    paint: def.outline.paint,
                    layout: { visibility: visible ? 'visible' : 'none' },
                },
                opts.beforeId,
            );
            added.push(mvtOutlineLayerId(def));
        }
    }

    return added;
}

/**
 * Apply a visibility map to layers already on the map.
 *
 * Separate from `addMvtLayers` because a toggle must not re-add sources —
 * that would drop MapLibre's tile cache and refetch on every click.
 */
export function setMvtVisibility(
    map: MvtCapableMap,
    visibleLayers: Record<string, boolean>,
    layers: MvtLayerDef[] = MVT_LAYERS,
): void {
    for (const def of layers) {
        const visibility = visibleLayers[def.id] ? 'visible' : 'none';
        for (const id of [mvtLayerId(def), mvtOutlineLayerId(def)]) {
            if (map.getLayer(id)) {
                map.setLayoutProperty(id, 'visibility', visibility);
            }
        }
    }
}

/**
 * Remove every layer and source this module added.
 *
 * Layers before sources: MapLibre refuses to remove a source that a layer
 * still references, and the outline layer shares its source with its fill.
 */
export function removeMvtLayers(map: MvtCapableMap, layers: MvtLayerDef[] = MVT_LAYERS): void {
    for (const def of layers) {
        for (const id of [mvtOutlineLayerId(def), mvtLayerId(def)]) {
            if (map.getLayer(id)) {
                map.removeLayer(id);
            }
        }
    }
    const sourceIds = new Set(layers.map((d) => mvtSourceId(d)));
    for (const sourceId of sourceIds) {
        if (map.getSource(sourceId)) {
            map.removeSource(sourceId);
        }
    }
}
