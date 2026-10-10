/**
 * AppLayout.test.tsx
 *
 * Security regression guard: AppLayout (and its UserMenu sub-component)
 * must NOT read auth tokens from localStorage. Auth rides the Sanctum
 * session cookie via `credentials: 'same-origin'` (types.ts:11-12).
 *
 * Approach: full render with Inertia + child component tree mocked.
 * The logout fetch is the only network call this file makes directly.
 *
 * Sign-out contract: the request carries the LIVE XSRF cookie token (never the
 * page's stale <meta> one), the page only leaves once the server has ended the
 * session, and it leaves by a FULL navigation so the layout (and its CSRF
 * meta, Echo connection, in-memory state) is rebuilt for the next sign-in.
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

// A hard navigation is captured, not performed (jsdom cannot navigate).
const realLocation = window.location;
let assign: ReturnType<typeof vi.fn>;
function stubLocation() {
    assign = vi.fn();
    Object.defineProperty(window, 'location', { configurable: true, value: { ...realLocation, assign } });
}
function restoreLocation() {
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
}
function clearCookies() {
    for (const part of document.cookie.split(';')) {
        const name = part.split('=')[0].trim();
        if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    }
}

describe('AppLayout — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        stubLocation();
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
        restoreLocation();
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
    beforeEach(() => {
        stubLocation();
        clearCookies();
        document.head.innerHTML = '';
    });

    afterEach(() => {
        vi.restoreAllMocks();
        restoreLocation();
        clearCookies();
        document.head.innerHTML = '';
    });

    function openAndSignOut() {
        const view = render(
            <AppLayout>
                <div />
            </AppLayout>,
        );
        fireEvent.click(view.getByRole('button', { expanded: false }));
        fireEvent.click(view.getByRole('menuitem', { name: /sign out/i }));
        return view;
    }

    it('navigates to /login only after the logout request has finished, and by a full navigation', async () => {
        const { router } = await import('@inertiajs/react');
        const visit = router.visit as unknown as ReturnType<typeof vi.fn>;
        visit.mockClear();
        let finish: (r: Response) => void = () => {};
        vi.spyOn(globalThis, 'fetch').mockReturnValue(
            new Promise<Response>((res) => {
                finish = res;
            }),
        );

        openAndSignOut();

        await new Promise((r) => setTimeout(r, 0));
        expect(assign).not.toHaveBeenCalled();

        finish(new Response('{}', { status: 200 }));
        await waitFor(() => expect(assign).toHaveBeenCalledWith('/login'));
        // An Inertia visit is an XHR: the blade layout (and its CSRF meta)
        // would not be re-rendered for the next sign-in.
        expect(visit).not.toHaveBeenCalled();
    });

    it('sends the live XSRF cookie token, not the stale csrf meta tag', async () => {
        // Rendered by the layout at page load; dead after an SPA sign-out/in.
        document.head.innerHTML = '<meta name="csrf-token" content="stale-from-page-load">';
        document.cookie = 'XSRF-TOKEN=live-token; path=/';
        const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 200 }));

        openAndSignOut();
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(url).toBe('/api/v1/auth/logout');
        const headers = init.headers as Record<string, string>;
        expect(headers['X-XSRF-TOKEN']).toBe('live-token');
        expect(headers).not.toHaveProperty('X-CSRF-TOKEN');
    });

    it.each([
        ['a 419 (CSRF mismatch)', () => Promise.resolve(new Response('{}', { status: 419 }))],
        ['a 500', () => Promise.resolve(new Response('{}', { status: 500 }))],
        ['a network failure', () => Promise.reject(new TypeError('Failed to fetch'))],
    ])('stays on the page and says so when sign-out fails with %s', async (_label, respond) => {
        const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(respond);
        const removeItem = vi.spyOn(Storage.prototype, 'removeItem');

        const view = openAndSignOut();

        await waitFor(() => expect(view.getByText('Could not sign out')).toBeInTheDocument());
        expect(view.getByText(/still signed in/i)).toBeInTheDocument();
        expect(fetchSpy).toHaveBeenCalledTimes(1);
        // The session is live, so nothing says it ended.
        expect(assign).not.toHaveBeenCalled();
        expect(removeItem).not.toHaveBeenCalledWith('georag_user');
    });

    it('treats a 401 as already signed out and leaves', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 401 }));

        openAndSignOut();

        await waitFor(() => expect(assign).toHaveBeenCalledWith('/login'));
    });

    it('lets the user try again after a failed sign-out', async () => {
        const fetchSpy = vi
            .spyOn(globalThis, 'fetch')
            .mockResolvedValueOnce(new Response('{}', { status: 500 }))
            .mockResolvedValueOnce(new Response('{}', { status: 200 }));

        const view = openAndSignOut();
        await waitFor(() => expect(view.getByText('Could not sign out')).toBeInTheDocument());
        expect(assign).not.toHaveBeenCalled();

        fireEvent.click(view.getByRole('menuitem', { name: /sign out/i }));
        await waitFor(() => expect(assign).toHaveBeenCalledWith('/login'));
        expect(fetchSpy).toHaveBeenCalledTimes(2);
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
