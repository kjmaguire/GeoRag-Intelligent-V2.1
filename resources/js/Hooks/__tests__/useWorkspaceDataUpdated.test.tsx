/**
 * FE-16 — a burst of workspace.data_updated events inside the debounce
 * window must fire ONE callback carrying every affected type, not just the
 * last event's.
 */
import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const channel = vi.hoisted(() => ({ handler: null as null | ((e: unknown) => void) }));
vi.mock('@/lib/echoChannel', () => ({
    listenPrivate: (_name: string, _event: string, cb: (e: unknown) => void) => {
        channel.handler = cb;
        return () => { channel.handler = null; };
    },
}));

import { mergeDataUpdatedEvents, useWorkspaceDataUpdated } from '../useWorkspaceDataUpdated';

const evt = (types: string[], run = 'r') => ({
    workspace_id: 'w', project_id: 'p-1', pipeline_run_id: run, affected_types: types, updated_at: 'now',
});

beforeEach(() => {
    vi.useFakeTimers();
    (window as unknown as { Echo: unknown }).Echo = {};
});
afterEach(() => {
    vi.useRealTimers();
    delete (window as unknown as { Echo?: unknown }).Echo;
});

describe('useWorkspaceDataUpdated', () => {
    it('merges affected_types across the debounce window', () => {
        const cb = vi.fn();
        renderHook(() => useWorkspaceDataUpdated('p-1', cb));
        channel.handler!(evt(['collars', 'assays'], 'r1'));
        vi.advanceTimersByTime(500);
        channel.handler!(evt(['reports'], 'r2'));
        vi.advanceTimersByTime(2000);

        expect(cb).toHaveBeenCalledTimes(1);
        expect(cb.mock.calls[0][0].affected_types).toEqual(['collars', 'assays', 'reports']);
        expect(cb.mock.calls[0][0].pipeline_run_id).toBe('r2');
    });

    it('starts a fresh set after firing', () => {
        const cb = vi.fn();
        renderHook(() => useWorkspaceDataUpdated('p-1', cb));
        channel.handler!(evt(['collars']));
        vi.advanceTimersByTime(2000);
        channel.handler!(evt(['reports']));
        vi.advanceTimersByTime(2000);
        expect(cb.mock.calls.map((c) => c[0].affected_types)).toEqual([['collars'], ['reports']]);
    });

    it('mergeDataUpdatedEvents de-duplicates', () => {
        expect(mergeDataUpdatedEvents(evt(['a', 'b']), evt(['b', 'c'])).affected_types).toEqual(['a', 'b', 'c']);
        expect(mergeDataUpdatedEvents(null, evt(['x'])).affected_types).toEqual(['x']);
    });
});
