/**
 * The CSRF header for same-origin fetch() calls comes from the XSRF-TOKEN
 * COOKIE, which Laravel rewrites on every stateful response — never from the
 * `<meta name="csrf-token">` the blade layout rendered once. After a SPA
 * sign-out/sign-in the meta tag is dead, Laravel prefers `X-CSRF-TOKEN` over
 * `X-XSRF-TOKEN`, and every call that read it 419'd and bounced to /login.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { csrfHeaders, xsrfToken } from '../csrf';

function clearCookies() {
    for (const part of document.cookie.split(';')) {
        const name = part.split('=')[0].trim();
        if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    }
}

beforeEach(() => {
    clearCookies();
    document.head.innerHTML = '';
});
afterEach(() => {
    clearCookies();
    document.head.innerHTML = '';
});

describe('xsrfToken / csrfHeaders', () => {
    it('sends no header when there is no token', () => {
        expect(xsrfToken()).toBeNull();
        expect(csrfHeaders()).toEqual({});
    });

    it('sends the cookie as X-XSRF-TOKEN, URL-decoded the way Laravel encodes it', () => {
        // Laravel writes the encrypted token base64 with `=` padding, URL-encoded.
        document.cookie = 'XSRF-TOKEN=eyJpdiI6ImFiYyJ9%3D%3D; path=/';
        expect(xsrfToken()).toBe('eyJpdiI6ImFiYyJ9==');
        expect(csrfHeaders()).toEqual({ 'X-XSRF-TOKEN': 'eyJpdiI6ImFiYyJ9==' });
    });

    it('finds the cookie among others and ignores one that merely ends in the same name', () => {
        document.cookie = 'laravel_session=abc; path=/';
        document.cookie = 'MY-XSRF-TOKEN=impostor; path=/';
        document.cookie = 'XSRF-TOKEN=real; path=/';
        document.cookie = 'theme=dark; path=/';
        expect(xsrfToken()).toBe('real');
    });

    it('reads the cookie on every call, so a rotated token is picked up without a reload', () => {
        document.cookie = 'XSRF-TOKEN=before-sign-out; path=/';
        expect(csrfHeaders()).toEqual({ 'X-XSRF-TOKEN': 'before-sign-out' });

        // Sign-out then sign-in: the session's token changes, the cookie follows.
        document.cookie = 'XSRF-TOKEN=after-sign-in; path=/';
        expect(csrfHeaders()).toEqual({ 'X-XSRF-TOKEN': 'after-sign-in' });
    });

    it('never falls back to the stale meta tag, and never sends X-CSRF-TOKEN', () => {
        document.head.innerHTML = '<meta name="csrf-token" content="stale-from-page-load">';
        document.cookie = 'XSRF-TOKEN=current; path=/';

        const headers = csrfHeaders();
        expect(headers).toEqual({ 'X-XSRF-TOKEN': 'current' });
        expect(headers).not.toHaveProperty('X-CSRF-TOKEN');
    });

    it('with no cookie, does not invent a header from the meta tag', () => {
        document.head.innerHTML = '<meta name="csrf-token" content="stale-from-page-load">';
        expect(csrfHeaders()).toEqual({});
    });

    it('treats a malformed percent-escape as no token instead of throwing', () => {
        document.cookie = 'XSRF-TOKEN=%E0%A4%A; path=/';
        expect(xsrfToken()).toBeNull();
        expect(csrfHeaders()).toEqual({});
    });
});
