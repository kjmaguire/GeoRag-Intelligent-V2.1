/**
 * useWorkspaceDataUpdated — Phase 2b of the ingestion-reliability spec.
 *
 * Subscribes to `project.{projectId}.ingestion` private Reverb channel
 * and fires `callback` when the `workspace.data_updated` event arrives.
 *
 * Distinct from the `ingestion.progress` listener wired into
 * IngestionRuns.tsx. That one fires on every per-run state transition
 * (started, completed, failed). This one fires ONCE per workspace after
 * BOTH (a) data_version was bumped AND (b) the per-completion MV
 * refresh succeeded. It is the "data is ready to re-query" signal.
 *
 * Behaviour contract (matches spec):
 *   1. Subscribe on mount, unsubscribe on unmount.
 *   2. Don't trigger a reload if the component is unmounted.
 *   3. Debounce duplicate events within a 2-second window — bursts of
 *      backend dispatches (or Echo reconnect replays) shouldn't cause
 *      racing Inertia requests.
 *   4. No-op when `window.Echo` is missing (SSR / test env / dev mode
 *      without Reverb).
 *
 * Usage:
 *
 *     useWorkspaceDataUpdated(projectId, (event) => {
 *       if (event.affected_types.includes('reports')) {
 *         router.reload({ only: ['documents'] });
 *       }
 *     });
 *
 * @see App\Events\WorkspaceDataUpdated for the emitter side.
 */

import { useEffect, useRef } from 'react';

export interface WorkspaceDataUpdatedEvent {
    workspace_id: string;
    project_id: string;
    pipeline_run_id: string;
    affected_types: string[];
    updated_at: string;
}

const DEBOUNCE_MS = 2000;

export function useWorkspaceDataUpdated(
    projectId: string | null | undefined,
    callback: (event: WorkspaceDataUpdatedEvent) => void,
): void {
    // Keep the latest callback in a ref so we don't re-subscribe every
    // time the parent component re-renders with a fresh closure.
    const callbackRef = useRef(callback);
    useEffect(() => {
        callbackRef.current = callback;
    }, [callback]);

    useEffect(() => {
        if (!projectId) return;
        if (typeof window === 'undefined') return;
        // graceful degradation per spec — no Reverb available = no-op.
        if (!window.Echo) return;

        let isMounted = true;
        let debounceTimer: ReturnType<typeof setTimeout> | null = null;
        let pendingEvent: WorkspaceDataUpdatedEvent | null = null;

        const channelName = `project.${projectId}.ingestion`;
        const ch = window.Echo.private(channelName);

        const fire = (): void => {
            if (!isMounted) return;
            if (pendingEvent === null) return;
            const event = pendingEvent;
            pendingEvent = null;
            try {
                callbackRef.current(event);
            } catch (err) {
                // Don't let a buggy reload handler tear down the subscription.
                console.error('useWorkspaceDataUpdated callback threw', err);
            }
        };

        ch.listen('.workspace.data_updated', (raw: unknown) => {
            if (!isMounted) return;
            const event = raw as WorkspaceDataUpdatedEvent;
            if (event.project_id !== projectId) return;

            // Always remember the latest event (so the trailing reload
            // works against the freshest payload), reset the timer on
            // every arrival — classic trailing-edge debounce.
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
