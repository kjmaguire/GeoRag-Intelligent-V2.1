/**
 * ProjectSelector.test.tsx
 *
 * Security regression guard: ProjectSelector must NOT read auth tokens from
 * localStorage. Its fetchProjects call uses Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';

// ── Inertia mock ───────────────────────────────────────────────────────────
// ProjectSelector reads `url` from usePage() to sync the dropdown to the
// current route's project slug. Outside an <App> tree usePage() throws, so
// mock it the same way MapView's tests do.
vi.mock('@inertiajs/react', () => ({
    usePage: () => ({ url: '/projects/pls' }),
    router: { visit: vi.fn() },
}));

// Import AFTER the mock is registered.
import ProjectSelector from '../ProjectSelector';

describe('ProjectSelector — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    const projectList = [
        { project_id: 'proj-001', project_name: 'Patterson Lake South', slug: 'pls' },
    ];

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({ data: projectList }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        fetchSpy.mockRestore();
    });

    it('does not read auth tokens from localStorage during project fetch', async () => {
        render(<ProjectSelector />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls
            .map(([key]) => String(key))
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('project fetch uses same-origin credentials', async () => {
        render(<ProjectSelector />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });
});
