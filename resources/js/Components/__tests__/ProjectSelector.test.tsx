/**
 * ProjectSelector.test.tsx
 *
 * Security regression guard: ProjectSelector must NOT read auth tokens from
 * localStorage. Its fetchProjects call uses Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// ── Inertia mock ───────────────────────────────────────────────────────────
// ProjectSelector reads `url` from usePage() to sync the dropdown to the
// current route's project slug. Outside an <App> tree usePage() throws, so
// mock it the same way MapView's tests do.
vi.mock('@inertiajs/react', () => ({
    usePage: () => ({ url: '/projects/pls' }),
    router: { visit: vi.fn() },
}));

// Import AFTER the mock is registered.
import ProjectSelector, { projectSwitchUrl } from '../ProjectSelector';

describe('ProjectSelector — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    const projectList = [{ project_id: 'proj-001', project_name: 'Patterson Lake South', slug: 'pls' }];

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
        const offending = getItemSpy.mock.calls.map(([key]) => String(key)).filter((k) => tokenLike.test(k));
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

describe('ProjectSelector — FE-14', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('Retry actually refetches after a failure', async () => {
        const fetchSpy = vi
            .spyOn(globalThis, 'fetch')
            .mockResolvedValueOnce(new Response('nope', { status: 500 }))
            .mockResolvedValueOnce(
                new Response(
                    JSON.stringify({
                        data: [{ project_id: 'proj-001', project_name: 'Patterson Lake South', slug: 'pls' }],
                    }),
                    { status: 200, headers: { 'Content-Type': 'application/json' } },
                ),
            );

        render(<ProjectSelector />);
        fireEvent.click(await screen.findByRole('button', { name: /retry/i }));

        await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
        expect(await screen.findByRole('option', { name: /Patterson Lake South/ })).toBeInTheDocument();
        expect(screen.queryByText(/loading projects/i)).toBeNull();
    });

    it.each([
        ['/projects/a/workspace?mode=3d', '/projects/b/workspace'],
        ['/projects/a/reports/5f0c-uuid', '/projects/b/reports'],
        ['/projects/a/holes/c-1/detail', '/projects/b'],
        ['/projects/a/imports/quality', '/projects/b/imports/quality'],
        ['/projects/a', '/projects/b'],
        ['/projects', '/projects/b'],
    ])('switching from %s goes to %s — no record ids across projects', (from, to) => {
        expect(projectSwitchUrl(from, 'b')).toBe(to);
    });
});
