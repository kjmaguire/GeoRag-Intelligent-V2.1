/**
 * Tile URL builder for silver (per-project) MVT tile sources.
 *
 * Produces URLs matching the Laravel TileProxyController contract (Chunk 8.4):
 *   GET /tiles/silver/{source}/{z}/{x}/{y}.pbf?project_id={uuid}&v={n}
 *
 * The `v` query parameter is a client-side cache-bust derived from the
 * workspace's `data_version`. Bumping it forces MapLibre to drop its
 * in-memory tile cache and re-fetch all tiles. The Laravel proxy ignores
 * this parameter for ETag derivation (server-side ETag is derived from
 * silver.projects.data_version); it exists purely as a client cache key.
 *
 * Module 8 Chunk 8.5.
 */

/**
 * Build a MapLibre tile URL template for a silver MVT source.
 *
 * @param functionName   Full Martin function name, e.g. 'pg_collars_by_project'
 * @param projectId      UUID of the active project
 * @param dataVersion    Workspace data_version from Inertia props (0 if unknown)
 * @returns              URL template string with {z}/{x}/{y} placeholders
 */
export function buildSilverTileUrl(
    functionName: string,
    projectId: string,
    dataVersion: number,
): string {
    // MapLibre fetches MVT tiles from a Web Worker. Workers have no document
    // base, so relative URLs fail `new Request()` with "Failed to parse URL".
    // Always emit an absolute URL bound to the page's origin.
    const origin = typeof window !== 'undefined' && window.location
        ? window.location.origin
        : '';
    return `${origin}/tiles/silver/${functionName}/{z}/{x}/{y}.pbf?project_id=${projectId}&v=${dataVersion}`;
}

/**
 * Build the full set of tile URL templates for all silver MVT sources,
 * keyed by the layer's `id` field from MVT_LAYERS.
 *
 * Convenience wrapper used by the data_version change effect in MapView
 * to swap all sources in one pass.
 *
 * @param layers        Array of MVT layer definitions (from mvtLayers.ts)
 * @param projectId     UUID of the active project
 * @param dataVersion   Workspace data_version (0 fallback)
 * @returns             Map of layerId → tile URL template string
 */
export function buildAllSilverTileUrls(
    layers: Array<{ id: string; functionName: string }>,
    projectId: string,
    dataVersion: number,
): Map<string, string> {
    const urls = new Map<string, string>();
    for (const layer of layers) {
        urls.set(layer.id, buildSilverTileUrl(layer.functionName, projectId, dataVersion));
    }
    return urls;
}
