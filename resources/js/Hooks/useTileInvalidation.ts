/**
 * useTileInvalidation — Phase 4 of the real-time staleness fix.
 *
 * Subscribes to `project.{projectId}.ingestion`, listens for
 * `.workspace.data_updated` events, and fires the callback with the
 * post-bump silver.projects.data_version.
 *
 * Behaviour contract — matches the Phase 1/2/3 hook family:
 *   1. Subscribe on mount, unsubscribe on unmount.
 *   2. Don't fire callback when unmounted.
 *   3. 2-second trailing debounce — bursts of completions collapse into
 *      a single setTiles() pass.
 *   4. No-op when `window.Echo` is missing (SSR / test env / dev without Reverb).
 *
 * @see App\Events\WorkspaceDataUpdated for the Silver emitter side.
 */

import { useEffect, useRef } from 'react';

import { listenPrivate } from '@/lib/echoChannel';

export interface SilverTileInvalidationEvent {
    workspace_id: string;
    project_id: string;
    pipeline_run_id: string;
    affected_types: string[];
    data_version: number | null;
    updated_at: string;
}

const DEBOUNCE_MS = 2000;

/**
 * Subscribe to the project-scoped Silver tile invalidation signal.
 *
 * The callback receives the post-bump data_version. When the event
 * carries `data_version: null` (non-ingestion writers piggy-backing on
 * the same channel), the callback is NOT fired — there's no new tile
 * version to apply.
 *
 * @param projectId    UUID of the project to subscribe for, or null/undefined
 *                     to skip subscription.
 * @param callback     Fired with the new data_version (and full event
 *                     payload) after the 2 s debounce.
 */
export function useSilverTileInvalidation(
    projectId: string | null | undefined,
    callback: (dataVersion: number, event: SilverTileInvalidationEvent) => void,
): void {
    const callbackRef = useRef(callback);
    useEffect(() => {
        callbackRef.current = callback;
    }, [callback]);

    useEffect(() => {
        if (!projectId) return;
        if (typeof window === 'undefined') return;
        if (!window.Echo) return;

        let isMounted = true;
        let debounceTimer: ReturnType<typeof setTimeout> | null = null;
        let pendingEvent: SilverTileInvalidationEvent | null = null;

        const channelName = `project.${projectId}.ingestion`;

        const fire = (): void => {
            if (!isMounted) return;
            const event = pendingEvent;
            pendingEvent = null;
            if (event === null) return;
            // Only fire when the broadcast actually carries a new version.
            // Phase 1/3 callers that piggy-back on this event without a
            // version send data_version=null — those don't drive tile
            // invalidation. (Silver MVT URLs need a real numeric version
            // for the &v= cache-bust to mean anything.)
            if (event.data_version === null) return;
            try {
                callbackRef.current(event.data_version, event);
            } catch (err) {
                console.error('useSilverTileInvalidation callback threw', err);
            }
        };

        // Ref-counted — MapView is rendered INSIDE pages that also run
        // useWorkspaceDataUpdated on this same channel, so an Echo.leave()
        // here (a chat message scrolling its inline map out of the tree,
        // a workspace sub-view toggle) used to unbind the page's own
        // ingest listener along with this one.
        const unsubscribe = listenPrivate(channelName, '.workspace.data_updated', (raw) => {
            if (!isMounted) return;
            const event = raw as SilverTileInvalidationEvent;
            if (event.project_id !== projectId) return;

            pendingEvent = event;
            if (debounceTimer !== null) {
                clearTimeout(debounceTimer);
            }
            debounceTimer = setTimeout(fire, DEBOUNCE_MS);
        });

        return (): void => {
            isMounted = false;
            if (debounceTimer !== null) {
                clearTimeout(debounceTimer);
                debounceTimer = null;
            }
            unsubscribe();
        };
    }, [projectId]);
}
