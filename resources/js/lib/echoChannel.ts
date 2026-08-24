/**
 * Reference-counted Laravel Echo subscriptions.
 *
 * THE BUG THIS EXISTS TO PREVENT
 *
 * `Echo.private(name)` is memoised per channel name — every caller gets
 * the SAME channel object, and each `.listen()` adds another handler to
 * it. `Echo.leave(name)` then unsubscribes that one shared object and
 * deletes it from the connector's registry, taking every other
 * subscriber's handlers with it. Silently: no error, no reconnect, the
 * listener simply never fires again.
 *
 * `project.{projectId}.ingestion` has four subscribers by design — the
 * shell's ingest toast bridge, the Ingestion Runs page, the workspace
 * data-updated hook and the map's tile-invalidation hook — and three of
 * them called `Echo.leave()` on unmount. The shell's bridge lives in the
 * persistent layout, so the FIRST in-project Inertia navigation tore its
 * subscription down and nothing re-created it: from that point on, an
 * ingest could finish and the "file finished ingesting" toast never
 * appeared again for the rest of the session.
 *
 * FoundryShell already worked around this with bare `stopListening`, and
 * its comment names the hazard. That fixes the teardown but leaks the
 * subscription — hop between five projects and five channels stay open.
 * Counting references gets both: N subscribers share one subscription,
 * and the last one out is the only one that leaves.
 *
 * Not a hook: MapView, the three hooks and the shell all need it, and two
 * of those subscribe outside a hook body.
 */

/** Live handler count per channel name. Absent === no subscribers. */
const refCounts = new Map<string, number>();

/**
 * Listen for `eventName` on the private channel `channelName`.
 *
 * @returns an unsubscribe function. Idempotent — calling it twice
 *          decrements the count once, so a double-invoked React cleanup
 *          (StrictMode) can't drop the count below the real subscriber
 *          count and leave the channel torn down under a live listener.
 *          A no-op when `window.Echo` is missing (SSR, tests, dev without
 *          Reverb), which is the same graceful degradation every caller
 *          already implemented for itself.
 */
export function listenPrivate(
    channelName: string,
    eventName: string,
    handler: (payload: unknown) => void,
): () => void {
    const echo = typeof window === 'undefined' ? undefined : window.Echo;
    if (!echo) return () => {};

    const channel = echo.private(channelName);
    channel.listen(eventName, handler);
    refCounts.set(channelName, (refCounts.get(channelName) ?? 0) + 1);

    let released = false;

    return function unsubscribe(): void {
        if (released) return;
        released = true;

        try {
            channel.stopListening(eventName, handler);
        } catch {
            // A sibling may already have left the channel outright — the
            // handler is gone either way, which is all we wanted.
        }

        const remaining = (refCounts.get(channelName) ?? 1) - 1;
        if (remaining > 0) {
            refCounts.set(channelName, remaining);
            return;
        }

        refCounts.delete(channelName);
        try {
            window.Echo?.leave(channelName);
        } catch {
            // Leaving is a courtesy to the connection, not a correctness
            // requirement; a failure here must not break unmount.
        }
    };
}

/**
 * Live subscriber count for a channel. Test-facing — the leak and the
 * over-release are both invisible from the outside otherwise.
 */
export function subscriberCount(channelName: string): number {
    return refCounts.get(channelName) ?? 0;
}

/** Test-facing reset so one spec's channels can't leak into the next. */
export function __resetEchoChannelRefCounts(): void {
    refCounts.clear();
}
