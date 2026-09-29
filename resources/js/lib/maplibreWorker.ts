/**
 * MapLibre GL 6 worker wiring.
 *
 * maplibre-gl 6 is ESM-only and locates its worker at
 * `./maplibre-gl-worker.mjs` relative to `import.meta.url`. Once Vite folds
 * maplibre-gl into a hashed chunk (`/build/assets/maplibre-gl-<hash>.js`) that
 * sibling file does not exist, so without an explicit `setWorkerUrl()` no
 * vector / GeoJSON tile is ever parsed and every source stays empty — with no
 * exception thrown at the call site.
 *
 * `?worker&url` (NOT plain `?url`) is required: the dist worker imports
 * `maplibre-gl-shared.mjs`, and `?url` would emit the worker without it.
 * `?worker&url` routes it through Vite's worker pipeline into a
 * self-contained chunk, served same-origin (so `worker-src 'self'` suffices).
 *
 * Call `configureMaplibreWorker(maplibregl)` once per module that constructs
 * a `Map`, before the first `new Map(...)`. It is idempotent.
 */
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';

export function configureMaplibreWorker(ml: { setWorkerUrl?: (url: string) => void }): void {
    // Some test doubles do not define setWorkerUrl; strict vi.mock() proxies
    // throw on access to a missing export, hence the try/catch.
    try {
        if (typeof ml.setWorkerUrl === 'function') ml.setWorkerUrl(workerUrl);
    } catch {
        /* no worker configuration available (test double) */
    }
}
