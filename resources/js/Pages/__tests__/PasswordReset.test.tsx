/**
 * Forgot / reset password — public pages, but their POSTs still go through
 * Sanctum's stateful middleware, so they need the CSRF token or 419. They
 * send the live XSRF cookie token (lib/csrf), not the <meta> one rendered at
 * page load, which is stale after a sign-out/sign-in in the same tab.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    Link: ({ href, children }: { href: string; children: ReactNode }) => <a href={href}>{children}</a>,
}));

import ForgotPassword from '../ForgotPassword';
import ResetPassword from '../ResetPassword';

function clearCookies() {
    for (const part of document.cookie.split(';')) {
        const name = part.split('=')[0].trim();
        if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    }
}

let fetchSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
    document.head.innerHTML = '<meta name="csrf-token" content="stale-from-page-load">';
    document.cookie = 'XSRF-TOKEN=live-token; path=/';
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 200 }));
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    clearCookies();
    document.head.innerHTML = '';
});

function sentHeaders(): Record<string, string> {
    return (fetchSpy.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
}

describe('ForgotPassword', () => {
    it('posts with the live XSRF cookie token, never the stale meta token', async () => {
        const { container } = render(<ForgotPassword />);
        fireEvent.change(container.querySelector('input[type="email"]') as HTMLInputElement, {
            target: { value: 'kyle@example.com' },
        });
        fireEvent.submit(container.querySelector('form') as HTMLFormElement);

        await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
        expect(fetchSpy.mock.calls[0][0]).toBe('/api/v1/auth/forgot-password');
        expect(sentHeaders()['X-XSRF-TOKEN']).toBe('live-token');
        expect(sentHeaders()).not.toHaveProperty('X-CSRF-TOKEN');
        await screen.findByText(/a password reset link has been sent/i);
    });
});

describe('ResetPassword', () => {
    it('posts with the live XSRF cookie token, never the stale meta token', async () => {
        const { container } = render(<ResetPassword token="reset-token" email="kyle@example.com" />);
        const passwords = container.querySelectorAll('input[type="password"]');
        fireEvent.change(passwords[0], { target: { value: 'a-new-password-1' } });
        fireEvent.change(passwords[1], { target: { value: 'a-new-password-1' } });
        fireEvent.submit(container.querySelector('form') as HTMLFormElement);

        await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
        expect(fetchSpy.mock.calls[0][0]).toBe('/api/v1/auth/reset-password');
        expect(sentHeaders()['X-XSRF-TOKEN']).toBe('live-token');
        expect(sentHeaders()).not.toHaveProperty('X-CSRF-TOKEN');
    });
});
