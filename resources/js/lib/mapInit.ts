/**
 * Why a MapLibre map could not start, in words a geologist can act on.
 *
 * `new maplibregl.Map()` throws when the browser cannot give it a WebGL 2
 * context: a remote desktop or VDI session with no GPU, hardware acceleration
 * switched off, a locked-down browser. Two map pages let that throw reach the
 * root error boundary, so the whole page went ("Something went wrong") for a
 * missing map, and the two that build their map after a dynamic import left a
 * silent blank panel. A failed import of the map chunk (a deploy replaced it
 * mid-session, a flaky network) is the other way a map does not start.
 */
export function mapStartFailure(error: unknown): string {
    const text = error instanceof Error ? `${error.name}: ${error.message}` : String(error);

    if (/webgl/i.test(text)) {
        return 'The map needs WebGL 2, which this browser or remote session does not provide. The rest of the page still works; try another browser, or turn on hardware acceleration.';
    }
    if (/dynamically imported module|importing a module script failed|loading chunk|failed to fetch/i.test(text)) {
        return 'The map could not be loaded. Reload the page to try again.';
    }
    return 'The map could not start. The rest of the page still works; reload the page to try again.';
}
