/**
 * useEventDedup — per-answer-run SSE event deduplication hook.
 *
 * Module 7 Chunk 4 — WS-01 frontend-side event replay dedup.
 *
 * Maintains a Set<string> of seen event_ids (UUID4) and a `lastSeq` integer
 * tracking the monotonic max event_seq observed. On reconnect, callers pass
 * `lastSeq` as the `since_event_seq` query param to the replay endpoint:
 *
 *   GET /v1/answer_runs/{id}/events?since_event_seq=<lastSeq>
 *
 * Dedup logic:
 *   - Primary key: event_id (UUID4 — guaranteed unique by EventStamper).
 *   - Fallback key when event_id absent (defensive): composite of
 *     `${answer_run_id}:${event_seq}` so partial/degraded events are still
 *     deduplicated.
 *   - Out-of-order delivery is safe because isDuplicate() checks the Set
 *     regardless of seq ordering.
 *
 * Usage:
 *   const { recordEvent, lastSeq, isDuplicate } = useEventDedup(answerRunId);
 *
 *   // In SSE listener:
 *   if (isDuplicate(event)) return;
 *   recordEvent(event);
 *   // ... apply event to state ...
 *
 *   // On reconnect:
 *   fetch(`/v1/answer_runs/${answerRunId}/events?since_event_seq=${lastSeq}`)
 *     .then(r => r.json())
 *     .then(events => events.forEach(e => { if (!isDuplicate(e)) { recordEvent(e); apply(e); } }))
 */

import { useRef, useCallback } from 'react';

export interface DeduplicableEvent {
    event_id?: string | null;
    event_seq?: number | null;
    answer_run_id?: string | null;
    [key: string]: unknown;
}

export interface UseEventDedupReturn {
    /**
     * Record that an event has been seen. Updates the seen-IDs set and
     * advances lastSeq if event_seq is greater than the current max.
     * Call this AFTER isDuplicate() returns false and BEFORE applying
     * the event to state.
     */
    recordEvent: (event: DeduplicableEvent) => void;

    /**
     * The highest event_seq observed so far (0 before any events seen).
     * Use as `since_event_seq` query param on the replay endpoint call.
     */
    lastSeq: number;

    /**
     * Returns true if this event has already been applied to state.
     * Check this BEFORE applying state mutations; skip the event if true.
     * Does NOT mutate the dedup state — call recordEvent() to do that.
     */
    isDuplicate: (event: DeduplicableEvent) => boolean;

    /**
     * Reset all dedup state (seen IDs + lastSeq). Call when starting a
     * fresh answer run so state from a previous run doesn't leak.
     */
    reset: () => void;
}

/**
 * Derive the dedup key for an event.
 *
 * Primary:  event_id (UUID4 from EventStamper — guaranteed unique).
 * Fallback: `${answer_run_id}:${event_seq}` — handles degraded payloads
 *           where EventStamper couldn't attach a full UUID (shouldn't
 *           happen in production but guards against rollout gaps).
 */
function deriveKey(event: DeduplicableEvent): string | null {
    if (event.event_id && typeof event.event_id === 'string' && event.event_id.length > 0) {
        return event.event_id;
    }
    // Fallback: composite key
    const runId = event.answer_run_id ?? 'unknown';
    const seq = event.event_seq;
    if (seq != null && typeof seq === 'number') {
        return `${runId}:${seq}`;
    }
    // No usable key — cannot dedup (caller should still apply, not skip)
    return null;
}

export function useEventDedup(_answerRunId: string | null): UseEventDedupReturn {
    // seenIds: Set of derived keys (event_id or composite fallback)
    const seenIds = useRef<Set<string>>(new Set());
    // lastSeq: monotonic max event_seq seen (mutable via ref for perf)
    const lastSeqRef = useRef<number>(0);

    const isDuplicate = useCallback((event: DeduplicableEvent): boolean => {
        const key = deriveKey(event);
        if (key === null) {
            // No dedup key available — treat as non-duplicate so we don't
            // silently drop events we can't identify.
            return false;
        }
        return seenIds.current.has(key);
    }, []);

    const recordEvent = useCallback((event: DeduplicableEvent): void => {
        const key = deriveKey(event);
        if (key !== null) {
            seenIds.current.add(key);
        }
        // Advance lastSeq if this event carries a higher seq
        if (event.event_seq != null && typeof event.event_seq === 'number') {
            if (event.event_seq > lastSeqRef.current) {
                lastSeqRef.current = event.event_seq;
            }
        }
    }, []);

    const reset = useCallback((): void => {
        seenIds.current = new Set();
        lastSeqRef.current = 0;
    }, []);

    // Expose lastSeq as a plain number getter so tests can read it.
    // We return the ref's value via a getter object to keep it live
    // without triggering re-renders on every seq advance.
    return {
        recordEvent,
        get lastSeq() { return lastSeqRef.current; },
        isDuplicate,
        reset,
    };
}
