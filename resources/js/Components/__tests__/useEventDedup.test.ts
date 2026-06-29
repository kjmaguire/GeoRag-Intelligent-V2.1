/**
 * useEventDedup.test.ts
 *
 * Tests for the WS-01 event deduplication hook.
 * Module 7 Chunk 4.
 *
 * Coverage:
 *   - First event is recorded (not a duplicate)
 *   - Duplicate detected by event_id
 *   - lastSeq tracks monotonic max
 *   - Replay after reconnect skips duplicates
 *   - Handles missing event_id (fallback composite key)
 *   - Handles out-of-order delivery
 *   - reset() clears all state
 *   - Event with no key at all treated as non-duplicate (safety valve)
 */

import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useEventDedup } from '../../Hooks/useEventDedup';
import type { DeduplicableEvent } from '../../Hooks/useEventDedup';

// ── Helpers ───────────────────────────────────────────────────────────────

function makeEvent(overrides: Partial<DeduplicableEvent> = {}): DeduplicableEvent {
    return {
        event_id: 'uuid-event-1',
        event_seq: 1,
        answer_run_id: 'run-aaa',
        event: 'delta',
        token: 'hello',
        ...overrides,
    };
}

// ── Tests ─────────────────────────────────────────────────────────────────

describe('useEventDedup — first event recorded', () => {
    it('first event is NOT a duplicate', () => {
        const { result } = renderHook(() => useEventDedup('run-1'));
        const event = makeEvent({ event_id: 'uuid-1', event_seq: 1 });
        expect(result.current.isDuplicate(event)).toBe(false);
    });

    it('after recordEvent, the same event IS a duplicate', () => {
        const { result } = renderHook(() => useEventDedup('run-1'));
        const event = makeEvent({ event_id: 'uuid-1', event_seq: 1 });
        act(() => { result.current.recordEvent(event); });
        expect(result.current.isDuplicate(event)).toBe(true);
    });
});

describe('useEventDedup — duplicate detection by event_id', () => {
    it('detects duplicate when event_id matches a previously recorded event', () => {
        const { result } = renderHook(() => useEventDedup('run-2'));
        const event1 = makeEvent({ event_id: 'uuid-dupe', event_seq: 5 });
        const event2 = makeEvent({ event_id: 'uuid-dupe', event_seq: 99 }); // different seq, same id
        act(() => { result.current.recordEvent(event1); });
        expect(result.current.isDuplicate(event2)).toBe(true);
    });

    it('two different event_ids are both non-duplicates', () => {
        const { result } = renderHook(() => useEventDedup('run-3'));
        const e1 = makeEvent({ event_id: 'uuid-A', event_seq: 1 });
        const e2 = makeEvent({ event_id: 'uuid-B', event_seq: 2 });
        act(() => { result.current.recordEvent(e1); });
        expect(result.current.isDuplicate(e2)).toBe(false);
    });
});

describe('useEventDedup — lastSeq monotonic tracking', () => {
    it('lastSeq starts at 0', () => {
        const { result } = renderHook(() => useEventDedup('run-seq'));
        expect(result.current.lastSeq).toBe(0);
    });

    it('lastSeq advances to event_seq after recordEvent', () => {
        const { result } = renderHook(() => useEventDedup('run-seq'));
        act(() => { result.current.recordEvent(makeEvent({ event_id: 'e1', event_seq: 7 })); });
        expect(result.current.lastSeq).toBe(7);
    });

    it('lastSeq does NOT decrease on lower event_seq (monotonic)', () => {
        const { result } = renderHook(() => useEventDedup('run-seq'));
        act(() => { result.current.recordEvent(makeEvent({ event_id: 'e1', event_seq: 10 })); });
        act(() => { result.current.recordEvent(makeEvent({ event_id: 'e2', event_seq: 3 })); });
        expect(result.current.lastSeq).toBe(10);
    });

    it('lastSeq tracks the highest event_seq seen across multiple events', () => {
        const { result } = renderHook(() => useEventDedup('run-seq'));
        [5, 2, 9, 1, 12, 4].forEach((seq, i) => {
            act(() => {
                result.current.recordEvent(makeEvent({ event_id: `e-${i}`, event_seq: seq }));
            });
        });
        expect(result.current.lastSeq).toBe(12);
    });
});

describe('useEventDedup — replay after reconnect skips duplicates', () => {
    it('events seen during live stream are skipped in replay', () => {
        const { result } = renderHook(() => useEventDedup('run-replay'));
        const liveEvent = makeEvent({ event_id: 'uuid-live', event_seq: 3 });

        // Simulate live stream
        act(() => { result.current.recordEvent(liveEvent); });

        // Simulate replay returning the same event
        expect(result.current.isDuplicate(liveEvent)).toBe(true);
    });

    it('new events from replay (not seen live) are not duplicates', () => {
        const { result } = renderHook(() => useEventDedup('run-replay'));
        const liveEvent = makeEvent({ event_id: 'uuid-live', event_seq: 3 });
        const replayOnlyEvent = makeEvent({ event_id: 'uuid-replay-only', event_seq: 4 });

        act(() => { result.current.recordEvent(liveEvent); });

        expect(result.current.isDuplicate(replayOnlyEvent)).toBe(false);
    });
});

describe('useEventDedup — fallback composite key (missing event_id)', () => {
    it('deduplicates by answer_run_id:event_seq when event_id is absent', () => {
        const { result } = renderHook(() => useEventDedup('run-fallback'));
        const event = makeEvent({ event_id: null, event_seq: 5, answer_run_id: 'run-fallback' });
        const duplicate = makeEvent({ event_id: null, event_seq: 5, answer_run_id: 'run-fallback' });

        act(() => { result.current.recordEvent(event); });
        expect(result.current.isDuplicate(duplicate)).toBe(true);
    });

    it('different event_seqs with missing event_id are NOT duplicates', () => {
        const { result } = renderHook(() => useEventDedup('run-fallback'));
        const e1 = makeEvent({ event_id: undefined, event_seq: 5, answer_run_id: 'run-fb' });
        const e2 = makeEvent({ event_id: undefined, event_seq: 6, answer_run_id: 'run-fb' });

        act(() => { result.current.recordEvent(e1); });
        expect(result.current.isDuplicate(e2)).toBe(false);
    });
});

describe('useEventDedup — out-of-order delivery', () => {
    it('handles events arriving out of order by event_seq', () => {
        const { result } = renderHook(() => useEventDedup('run-ooo'));
        const events = [
            makeEvent({ event_id: 'eid-5', event_seq: 5 }),
            makeEvent({ event_id: 'eid-2', event_seq: 2 }),
            makeEvent({ event_id: 'eid-9', event_seq: 9 }),
        ];
        events.forEach((e) => {
            act(() => { result.current.recordEvent(e); });
        });
        // All three should be seen
        events.forEach((e) => {
            expect(result.current.isDuplicate(e)).toBe(true);
        });
        // lastSeq should be 9 (highest)
        expect(result.current.lastSeq).toBe(9);
    });
});

describe('useEventDedup — event with no key at all', () => {
    it('treats an event with no event_id and no event_seq as non-duplicate (safety valve)', () => {
        const { result } = renderHook(() => useEventDedup('run-nokey'));
        const event = { event: 'status', message: 'hello' }; // no event_id, no event_seq
        // Should NOT throw and should return false (non-duplicate)
        expect(result.current.isDuplicate(event)).toBe(false);
    });
});

describe('useEventDedup — reset()', () => {
    it('reset() clears all seen IDs so previously seen events are non-duplicates again', () => {
        const { result } = renderHook(() => useEventDedup('run-reset'));
        const event = makeEvent({ event_id: 'uuid-reset', event_seq: 3 });
        act(() => { result.current.recordEvent(event); });
        expect(result.current.isDuplicate(event)).toBe(true);

        act(() => { result.current.reset(); });

        expect(result.current.isDuplicate(event)).toBe(false);
        expect(result.current.lastSeq).toBe(0);
    });
});
