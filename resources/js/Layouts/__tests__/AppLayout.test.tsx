/**
 * AppLayout.test.tsx
 *
 * Security regression guard: AppLayout (and its UserMenu sub-component)
 * must NOT read auth tokens from localStorage. Auth rides the Sanctum
 * session cookie via `credentials: 'same-origin'` (types.ts:11-12).
 *
 * Approach: full render with Inertia + child component tree mocked.
 * The logout fetch is the only network call this file makes directly.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';

// Mock Inertia — AppLayout reads usePage() for auth.user and url.
vi.mock('@inertiajs/react', () => ({
    usePage: vi.fn(() => ({
        props: { auth: { user: { name: 'Kyle', email: 'k@example.com' } } },
        url: '/chat',
    })),
    Link: ({
        href,
        children,
        className,
        onClick,
    }: {
        href: string;
        children?: ReactNode;
        className?: string;
        onClick?: () => void;
    }) => (
        <a href={href} className={className} onClick={onClick}>
            {children}
        </a>
    ),
    router: { visit: vi.fn() },
}));

// Mock ProjectSelector — it makes its own fetch; isolate to AppLayout surface only.
vi.mock('../../Components/ProjectSelector', () => ({
    default: () => <div data-testid="project-selector-stub" />,
}));

import AppLayout from '../AppLayout';

describe('AppLayout — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({}), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        fetchSpy.mockRestore();
    });

    it('does not read auth tokens from localStorage on mount', () => {
        render(
            <AppLayout>
                <div />
            </AppLayout>,
        );

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls.map(([key]) => String(key)).filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('does not read auth tokens from localStorage when logout is triggered', async () => {
        const { getByRole } = render(
            <AppLayout>
                <div />
            </AppLayout>,
        );

        // FoundryShell's UserMenu hides logout behind a collapsed dropdown
        // anchored on the user-initials button (haspopup). Open it first.
        fireEvent.click(getByRole('button', { expanded: false }));
        fireEvent.click(getByRole('menuitem', { name: /sign out/i }));

        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls.map(([key]) => String(key)).filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('logout fetch uses same-origin credentials', async () => {
        const { getByRole } = render(
            <AppLayout>
                <div />
            </AppLayout>,
        );

        fireEvent.click(getByRole('button', { expanded: false }));
        fireEvent.click(getByRole('menuitem', { name: /sign out/i }));
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });
});

describe('AppLayout — FE-20 / FE-24', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('navigates to /login only after the logout request has finished', async () => {
        const { router } = await import('@inertiajs/react');
        const visit = router.visit as unknown as ReturnType<typeof vi.fn>;
        visit.mockClear();
        let finish: (r: Response) => void = () => {};
        vi.spyOn(globalThis, 'fetch').mockReturnValue(
            new Promise<Response>((res) => {
                finish = res;
            }),
        );

        const { getByRole } = render(
            <AppLayout>
                <div />
            </AppLayout>,
        );
        fireEvent.click(getByRole('button', { expanded: false }));
        fireEvent.click(getByRole('menuitem', { name: /sign out/i }));

        await new Promise((r) => setTimeout(r, 0));
        expect(visit).not.toHaveBeenCalled();

        finish(new Response('{}', { status: 200 }));
        await waitFor(() => expect(visit).toHaveBeenCalledWith('/login'));
    });

    it('labels the theme toggle and reports which theme is on', () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 200 }));
        const { getByRole } = render(
            <AppLayout>
                <div />
            </AppLayout>,
        );
        const dark = getByRole('button', { name: 'Dark theme' });
        const light = getByRole('button', { name: 'Light theme' });
        expect([dark.getAttribute('aria-pressed'), light.getAttribute('aria-pressed')].sort()).toEqual([
            'false',
            'true',
        ]);
    });
});
