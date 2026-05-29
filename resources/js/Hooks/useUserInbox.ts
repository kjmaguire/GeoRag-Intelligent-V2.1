/**
 * useUserInbox — Phase 3 of the real-time staleness fix.
 *
 * Subscribes to the user's default Laravel private channel
 * (`App.Models.User.{userId}`) and fires `callback` when an inbox event
 * arrives. The same channel is used by Laravel's default broadcast
 * notifications, so this listener coexists with `Notification::send($user, ...)`
 * patterns.
 *
 * Used by Foundry/Inbox + nav-bar inbox badge:
 *   - The Inbox page calls router.reload({ only: ['mentions','reviews','refusals','empty'] })
 *   - The nav badge increments its counter from event.count_delta without
 *     a full page reload (instant feedback while still landing on the
 *     authoritative list on next reload).
 *
 * Event kinds match App\Events\User\UserInboxUpdated::KIND_*:
 *   - 'mention' — silver.collaboration_mentions insert
 *   - 'review'  — silver.collaboration_review_requests insert
 *   - 'refusal' — audit.query_audit_log terminal NULL response_text
 *
 * Behaviour contract (matches Phase 1/2/3 hook family): 2 s debounce,
 * unmount-safe, no-op without Reverb.
 *
 * @see App\Events\User\UserInboxUpdated for the emitter side.
 */

import { useEffect, useRef } from 'react';

export type InboxKind = 'mention' | 'review' | 'refusal';

export interface UserInboxEvent {
    user_id: number;
    kind: InboxKind;
    count_delta: number;
    payload: Record<string, unknown>;
    updated_at: string;
}

const DEBOUNCE_MS = 2000;

export function useUserInbox(
    userId: number | null | undefined,
    callback: (event: UserInboxEvent) => void,
): void {
    const callbackRef = useRef(callback);
    useEffect(() => {
        callbackRef.current = callback;
    }, [callback]);

    useEffect(() => {
        if (!userId) return;
        if (typeof window === 'undefined') return;
        if (!window.Echo) return;

        let isMounted = true;
        let debounceTimer: ReturnType<typeof setTimeout> | null = null;
        let pendingEvent: UserInboxEvent | null = null;

        const channelName = `App.Models.User.${userId}`;
        const ch = window.Echo.private(channelName);

        const fire = (): void => {
            if (!isMounted) return;
            if (pendingEvent === null) return;
            const event = pendingEvent;
            pendingEvent = null;
            try {
                callbackRef.current(event);
            } catch (err) {
                console.error('useUserInbox callback threw', err);
            }
        };

        ch.listen('.user.inbox_updated', (raw: unknown) => {
            if (!isMounted) return;
            const event = raw as UserInboxEvent;
            if (event.user_id !== userId) return;

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
    }, [userId]);
}
