/**
 * useAdminSurfaceUpdated — Phase 2 of the real-time staleness fix.
 *
 * Subscribes to the appropriate admin private Reverb channel and fires
 * `callback` whenever the `admin.surface_updated` event arrives for the
 * given surface (and surface_id, if any).
 *
 * Channel naming matches App\Events\Admin\AdminSurfaceUpdated:
 *   - list pages:        `admin.<surface>`
 *   - drilldown pages:   `admin.<surface>.<surface_id>`
 *
 * Behaviour contract (mirrors useWorkspaceDataUpdated):
 *   1. Subscribe on mount, unsubscribe on unmount.
 *   2. Don't trigger reload if the component is unmounted.
 *   3. Debounce duplicate events within a 2-second window — bursts of
 *      backend dispatches shouldn't cause racing Inertia requests.
 *   4. No-op when `window.Echo` is missing (SSR / test env / dev mode
 *      without Reverb).
 *
 * Usage:
 *
 *     // List page — no surface_id
 *     useAdminSurfaceUpdated('workflow-runs', null, () => {
 *         router.reload({ only: ['workflow_runs'] });
 *     });
 *
 *     // Drilldown page — surface_id passed
 *     useAdminSurfaceUpdated('target-run', run.run_id, () => {
 *         router.reload({ only: ['run'] });
 *     });
 *
 * @see App\Events\Admin\AdminSurfaceUpdated for the emitter side.
 */

import { useEffect, useRef } from 'react';

export interface AdminSurfaceUpdatedEvent {
    surface: string;
    surface_id: string | null;
    affected_props: string[];
    payload: Record<string, unknown>;
    timestamp: string;
}

const DEBOUNCE_MS = 2000;

export function useAdminSurfaceUpdated(
    surface: string,
    surfaceId: string | null | undefined,
    callback: (event: AdminSurfaceUpdatedEvent) => void,
): void {
    // Keep the latest callback in a ref so we don't re-subscribe on every
    // parent re-render.
    const callbackRef = useRef(callback);
    useEffect(() => {
        callbackRef.current = callback;
    }, [callback]);

    useEffect(() => {
        if (!surface) return;
        if (typeof window === 'undefined') return;
        if (!window.Echo) return;  // graceful no-op in SSR / dev without Reverb

        let isMounted = true;
        let debounceTimer: ReturnType<typeof setTimeout> | null = null;
        let pendingEvent: AdminSurfaceUpdatedEvent | null = null;

        const channelName = surfaceId
            ? `admin.${surface}.${surfaceId}`
            : `admin.${surface}`;
        const ch = window.Echo.private(channelName);

        const fire = (): void => {
            if (!isMounted) return;
            if (pendingEvent === null) return;
            const event = pendingEvent;
            pendingEvent = null;
            try {
                callbackRef.current(event);
            } catch (err) {
                // Don't tear down the subscription on a buggy reload handler.
                console.error('useAdminSurfaceUpdated callback threw', err);
            }
        };

        ch.listen('.admin.surface_updated', (raw: unknown) => {
            if (!isMounted) return;
            const event = raw as AdminSurfaceUpdatedEvent;
            // Defensive surface check — the channel already scopes us
            // but a server-side discriminator bug shouldn't fire callbacks
            // for the wrong page.
            if (event.surface !== surface) return;
            if (surfaceId && event.surface_id !== surfaceId) return;

            // Trailing-edge debounce: remember the latest event, reset the
            // timer on every arrival.
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
    }, [surface, surfaceId]);
}
