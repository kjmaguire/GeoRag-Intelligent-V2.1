/**
 * useDashboardFetch.test.ts
 *
 * Security regression guard: useDashboardFetch must NOT read auth tokens from
 * localStorage. Auth rides the Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useDashboardFetch } from '../useDashboardFetch';

describe('useDashboardFetch — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(
                JSON.stringify({ data: { items: [] }, generated_at: '2026-04-27T00:00:00Z' }),
                { status: 200, headers: { 'Content-Type': 'application/json' } },
            ),
        );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        fetchSpy.mockRestore();
    });

    it('does not read auth tokens from localStorage on fetch', async () => {
        renderHook(() => useDashboardFetch('/api/v1/dashboard/portfolio/projects'));
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls
            .map(([key]) => String(key))
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('fetch uses same-origin credentials', async () => {
        renderHook(() => useDashboardFetch('/api/v1/dashboard/portfolio/projects'));
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });
});
