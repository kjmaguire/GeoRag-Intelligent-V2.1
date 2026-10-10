/**
 * Login — Sanctum SPA sign-in. Pins the CSRF handshake: prime the XSRF cookie,
 * then sign in with that live token (never the <meta> one rendered at page load).
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const inertia = vi.hoisted(() => ({ visit: vi.fn() }));
vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    router: { visit: inertia.visit },
    usePage: () => ({ props: { app: { env: 'production' } } }),
}));

import Login from '../Login';

function clearCookies() {
    for (const part of document.cookie.split(';')) {
        const name = part.split('=')[0].trim();
        if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    }
}

beforeEach(() => {
    clearCookies();
    document.head.innerHTML = '';
    inertia.visit.mockClear();
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    clearCookies();
    document.head.innerHTML = '';
});

function fillAndSubmit(container: HTMLElement) {
    fireEvent.change(screen.getByLabelText('Work email'), { target: { value: 'kyle@example.com' } });
    fireEvent.change(container.querySelector('#password') as HTMLInputElement, { target: { value: 'hunter2hunter2' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
}

describe('Login', () => {
    it('primes the XSRF cookie, then signs in with that token and goes on to the projects list', async () => {
        // A stale token rendered at page load must never be the one sent.
        document.head.innerHTML = '<meta name="csrf-token" content="stale-from-page-load">';
        const calls: Array<{ url: string; init?: RequestInit }> = [];
        vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
            const url = String(input);
            calls.push({ url, init });
            if (url === '/sanctum/csrf-cookie') {
                // What Laravel's Set-Cookie does.
                document.cookie = 'XSRF-TOKEN=primed%3D; path=/';
                return new Response(null, { status: 204 });
            }
            return new Response(JSON.stringify({ user: { id: 1 } }), { status: 200 });
        });

        const { container } = render(<Login />);
        fillAndSubmit(container);

        await waitFor(() => expect(inertia.visit).toHaveBeenCalledWith('/projects'));
        expect(calls.map((c) => c.url)).toEqual(['/sanctum/csrf-cookie', '/api/v1/auth/spa-login']);
        const headers = calls[1].init?.headers as Record<string, string>;
        expect(headers['X-XSRF-TOKEN']).toBe('primed=');
        expect(headers).not.toHaveProperty('X-CSRF-TOKEN');
        expect(JSON.parse(String(calls[1].init?.body))).toEqual({
            email: 'kyle@example.com',
            password: 'hunter2hunter2',
        });
    });

    it('shows the server message and stays put when the credentials are refused', async () => {
        vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) =>
            String(input) === '/sanctum/csrf-cookie'
                ? new Response(null, { status: 204 })
                : new Response(JSON.stringify({ message: 'Invalid credentials.' }), { status: 401 }),
        );

        const { container } = render(<Login />);
        fillAndSubmit(container);

        expect(await screen.findByRole('alert')).toHaveTextContent('Invalid credentials.');
        expect(inertia.visit).not.toHaveBeenCalled();
    });
});
