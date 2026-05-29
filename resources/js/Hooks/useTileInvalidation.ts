/**
 * useTileInvalidation — Phase 4 of the real-time staleness fix.
 *
 * Two named exports for the two tile families:
 *
 *   useSilverTileInvalidation(projectId, callback)
 *     Subscribes to `project.{projectId}.ingestion` (existing Phase 1 channel),
 *     listens for `.workspace.data_updated` events, and fires `callback`
 *     with the post-bump silver.projects.data_version. MapView uses this
 *     to call setTiles() on every MVT source with the new `?v={n}` cache-bust.
 *
 *   usePublicGeoscienceTileInvalidation(callback)
 *     Subscribes to `public-geoscience.tiles` (new Phase 4 channel),
 *     listens for `.public_geoscience.tiles_invalidated` events, and
 *     fires `callback` with the post-write jurisdiction_epoch +
 *     optional source_ids subset. PublicGeoscienceMap uses this to call
 *     setTiles() on the affected PGEO sources with the new `?v={epoch}`.
 *
 * Behaviour contract — matches the Phase 1/2/3 hook family:
 *   1. Subscribe on mount, unsubscribe on unmount.
 *   2. Don't fire callback when unmounted.
 *   3. 2-second trailing debounce — bursts of completions collapse into
 *      a single setTiles() pass.
 *   4. No-op when `window.Echo` is missing (SSR / test env / dev without Reverb).
 *
 * @see App\Events\WorkspaceDataUpdated for the Silver emitter side.
 * @see App\Events\Map\PublicGeoscienceTilesInvalidated for the PGEO emitter side.
 */

import { useEffect, useRef } from 'react';

export interface SilverTileInvalidationEvent {
    workspace_id: string;
    project_id: string;
    pipeline_run_id: string;
    affected_types: string[];
    data_version: number | null;
    updated_at: string;
}

export interface PublicGeoscienceTileInvalidationEvent {
    jurisdiction_epoch: number;
    source_ids: string[] | null;
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
        const ch = window.Echo.private(channelName);

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

        ch.listen('.workspace.data_updated', (raw: unknown) => {
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
            window.Echo?.leave(channelName);
        };
    }, [projectId]);
}

/**
 * Subscribe to the workspace-global Public-Geoscience tile invalidation signal.
 *
 * @param callback   Fired with the post-write jurisdiction_epoch and
 *                   optional source_ids subset (null = all PGEO sources).
 */
export function usePublicGeoscienceTileInvalidation(
    callback: (
        jurisdictionEpoch: number,
        sourceIds: string[] | null,
        event: PublicGeoscienceTileInvalidationEvent,
    ) => void,
): void {
    const callbackRef = useRef(callback);
    useEffect(() => {
        callbackRef.current = callback;
    }, [callback]);

    useEffect(() => {
        if (typeof window === 'undefined') return;
        if (!window.Echo) return;

        let isMounted = true;
        let debounceTimer: ReturnType<typeof setTimeout> | null = null;
        let pendingEvent: PublicGeoscienceTileInvalidationEvent | null = null;

        const channelName = 'public-geoscience.tiles';
        const ch = window.Echo.private(channelName);

        const fire = (): void => {
            if (!isMounted) return;
            const event = pendingEvent;
            pendingEvent = null;
            if (event === null) return;
            try {
                callbackRef.current(event.jurisdiction_epoch, event.source_ids, event);
            } catch (err) {
                console.error('usePublicGeoscienceTileInvalidation callback threw', err);
            }
        };

        ch.listen('.public_geoscience.tiles_invalidated', (raw: unknown) => {
            if (!isMounted) return;
            const event = raw as PublicGeoscienceTileInvalidationEvent;
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
            window.Echo?.leave(channelName);
        };
    }, []);
}
