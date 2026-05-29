/**
 * useWorkspaceActivity — Phase 3 of the real-time staleness fix.
 *
 * Subscribes to `workspace.{workspaceId}.activity` private Reverb channel
 * and fires `callback` whenever a `workspace.activity` event arrives that
 * the receiving page cares about.
 *
 * Distinct from `useWorkspaceDataUpdated`:
 *   - useWorkspaceDataUpdated listens on `project.{projectId}.ingestion`
 *     (per-project channel). Used by Foundry/Overview, Lakehouse, etc.
 *   - useWorkspaceActivity listens on `workspace.{workspaceId}.activity`
 *     (cross-project channel). Used by Foundry/Portfolio, Foundry/Projects
 *     — pages that aggregate over every project in the workspace.
 *
 * Behaviour contract (matches the Phase 1/2 hook family):
 *   1. Subscribe on mount, unsubscribe on unmount.
 *   2. Don't trigger reload if the component is unmounted.
 *   3. 2-second trailing debounce — bursts of project completions inside
 *      one workspace collapse into a single partial reload.
 *   4. No-op when `window.Echo` is missing (SSR / test env / dev without Reverb).
 *
 * Usage:
 *
 *     useWorkspaceActivity(workspace.id, (event) => {
 *         if (event.affected_types.includes('projects')) {
 *             router.reload({ only: ['projects', 'kpis'] });
 *         }
 *     });
 *
 * @see App\Events\Workspace\WorkspaceActivityBroadcast for the emitter side.
 */

import { useEffect, useRef } from 'react';

export interface WorkspaceActivityEvent {
    workspace_id: string;
    affected_types: string[];
    payload: Record<string, unknown>;
    updated_at: string;
}

const DEBOUNCE_MS = 2000;

export function useWorkspaceActivity(
    workspaceId: string | null | undefined,
    callback: (event: WorkspaceActivityEvent) => void,
): void {
    const callbackRef = useRef(callback);
    useEffect(() => {
        callbackRef.current = callback;
    }, [callback]);

    useEffect(() => {
        if (!workspaceId) return;
        if (typeof window === 'undefined') return;
        if (!window.Echo) return;

        let isMounted = true;
        let debounceTimer: ReturnType<typeof setTimeout> | null = null;
        let pendingEvent: WorkspaceActivityEvent | null = null;

        const channelName = `workspace.${workspaceId}.activity`;
        const ch = window.Echo.private(channelName);

        const fire = (): void => {
            if (!isMounted) return;
            if (pendingEvent === null) return;
            const event = pendingEvent;
            pendingEvent = null;
            try {
                callbackRef.current(event);
            } catch (err) {
                console.error('useWorkspaceActivity callback threw', err);
            }
        };

        ch.listen('.workspace.activity', (raw: unknown) => {
            if (!isMounted) return;
            const event = raw as WorkspaceActivityEvent;
            if (event.workspace_id !== workspaceId) return;

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
    }, [workspaceId]);
}
