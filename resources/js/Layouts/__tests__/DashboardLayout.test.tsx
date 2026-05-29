/**
 * DashboardLayout.test.tsx
 *
 * Security regression guard: DashboardLayout's fetchProjects call must NOT
 * read auth tokens from localStorage. Auth rides the Sanctum session cookie
 * via `credentials: 'same-origin'` (types.ts:11-12).
 *
 * Note: DashboardLayout.UserChip reads `georag_user` (user display name, not
 * an auth token) from localStorage — that key does NOT match /token|jwt|secret/i
 * and is intentionally left in place.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';

// Mock Inertia — DashboardLayout uses Head, Link, router.
vi.mock('@inertiajs/react', () => ({
    Head: ({ children }: any) => <>{children}</>,
    Link: ({ href, children, className }: any) => (
        <a href={href} className={className}>{children}</a>
    ),
    router: { visit: vi.fn() },
}));

import DashboardLayout from '../DashboardLayout';

describe('DashboardLayout — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({ data: [] }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        fetchSpy.mockRestore();
    });

    it('does not read auth tokens from localStorage during fetchProjects', async () => {
        render(<DashboardLayout><div /></DashboardLayout>);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls
            .map(([key]) => String(key))
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('fetchProjects uses same-origin credentials', async () => {
        render(<DashboardLayout><div /></DashboardLayout>);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });
});
