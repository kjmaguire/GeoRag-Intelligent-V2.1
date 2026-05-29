/**
 * Tile failure watchdog — MAPVIEW-03 (Module 8 Chunk 8.7).
 *
 * Aggregates MapLibre tile-load errors per source within a sliding window.
 * When a source accumulates ≥ FAILURE_THRESHOLD failures within WINDOW_MS,
 * the watchdog fires a toast callback and resets the counter to prevent spam.
 *
 * Design notes:
 * - Pure function module — no React, no MapLibre imports. Fully unit-testable.
 * - 204 responses from Martin (empty tile) are NOT errors — don't call recordFailure
 *   for those. The caller (MapView) is responsible for filtering.
 * - A single 404 (possible empty tile from sparse data) does NOT trip the threshold.
 * - On successful tile load (sourcedata with tile.state === 'loaded'), call
 *   recordSuccess to decrement the counter and stop false positives.
 */

export const FAILURE_THRESHOLD = 3;
export const WINDOW_MS = 30_000; // 30 seconds

export interface SourceFailureEntry {
    sourceId: string;
    failures: number;
    firstAt: number;
}

export interface WatchdogOptions {
    /** Called once per threshold breach, then counter resets. */
    onThreshold: (sourceId: string, count: number, urlPrefix: string) => void;
    /** Override Date.now() for testability. */
    now?: () => number;
}

/**
 * Create a stateful watchdog instance. Call the returned functions from
 * MapLibre event handlers. Each MapView mounts its own instance.
 */
export function createTileFailureWatchdog(opts: WatchdogOptions) {
    const { onThreshold, now = () => Date.now() } = opts;

    const state = new Map<string, SourceFailureEntry>();

    /**
     * Build the proxy URL prefix shown in the toast so developers can
     * copy-paste it to diagnose the failing Martin function.
     *
     * Martin tile URLs look like:
     *   /tiles/silver/pg_collars_by_project/{z}/{x}/{y}.pbf?project_id=...
     *
     * We strip the tile coords template and keep the path prefix.
     */
    function urlPrefix(sourceId: string): string {
        // sourceId pattern: mvt-{layerId}-source  e.g. mvt-collars-source
        // Map to the URL prefix pattern: /tiles/silver/{functionName}/
        // We don't have the functionName here — use the sourceId as-is for simplicity.
        // The toast also accepts a raw prefix string built in MapView.
        return `/tiles/silver/ [source: ${sourceId}]`;
    }

    /**
     * Record one tile-load failure for a source.
     *
     * @param sourceId  MapLibre source id (e.g. 'mvt-collars-source')
     * @param tileUrlPrefix  The URL prefix from the tile request (for toast display)
     */
    function recordFailure(sourceId: string, tileUrlPrefix = ''): void {
        const currentTime = now();
        const entry = state.get(sourceId);

        if (!entry || currentTime - entry.firstAt > WINDOW_MS) {
            // No entry or window expired — start fresh
            state.set(sourceId, { sourceId, failures: 1, firstAt: currentTime });
            return;
        }

        const newCount = entry.failures + 1;
        if (newCount >= FAILURE_THRESHOLD) {
            // Threshold breached — fire toast and reset
            const prefix = tileUrlPrefix || urlPrefix(sourceId);
            onThreshold(sourceId, newCount, prefix);
            // Reset so we don't spam — next breach requires a fresh window
            state.delete(sourceId);
        } else {
            state.set(sourceId, { ...entry, failures: newCount });
        }
    }

    /**
     * Record a successful tile load for a source — resets its failure counter.
     * Call this from the `sourcedata` event when `tile.state === 'loaded'`.
     */
    function recordSuccess(sourceId: string): void {
        state.delete(sourceId);
    }

    /**
     * Read current state for a source (used in tests).
     */
    function getEntry(sourceId: string): SourceFailureEntry | undefined {
        return state.get(sourceId);
    }

    /**
     * Reset all state (used in tests / cleanup).
     */
    function reset(): void {
        state.clear();
    }

    return { recordFailure, recordSuccess, getEntry, reset };
}

export type TileFailureWatchdog = ReturnType<typeof createTileFailureWatchdog>;
