/**
 * bootstrap.ts is an entry module with side effects: it wraps window.fetch so
 * a 401/419 from our own origin bounces to /login, and builds the Echo client.
 * Both are exercised by importing it fresh against a stand-in fetch.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Echo opens a socket on construction; a stand-in that records its options.
const echo = vi.hoisted(() => ({ options: [] as unknown[] }));
vi.mock('laravel-echo', () => ({
    default: function EchoStandIn(this: unknown, options: unknown) {
        echo.options.push(options);
    },
}));
vi.mock('pusher-js', () => ({ default: function PusherStandIn() {} }));

const realFetch = window.fetch;
const realLocation = window.location;
let hrefSets: string[];

function clearCookies() {
    for (const part of document.cookie.split(';')) {
        const name = part.split('=')[0].trim();
        if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    }
}

/** Import bootstrap.ts afresh, so it wraps THIS test's stand-in fetch. */
async function boot(respondWith: () => Response) {
    window.fetch = vi.fn(async () => respondWith()) as unknown as typeof fetch;
    await import('../../bootstrap');
}

beforeEach(() => {
    vi.resetModules();
    vi.stubEnv('VITE_REVERB_APP_KEY', 'test-key');
    echo.options.length = 0;
    hrefSets = [];
    clearCookies();
    document.head.innerHTML = '';
    // Capture the hard navigation instead of performing it.
    Object.defineProperty(window, 'location', {
        configurable: true,
        value: {
            ...realLocation,
            origin: 'http://localhost',
            hostname: 'localhost',
            protocol: 'http:',
            pathname: '/projects/red-star/chat',
            search: '',
            set href(v: string) {
                hrefSets.push(v);
            },
            get href() {
                return 'http://localhost/projects/red-star/chat';
            },
        },
    });
});

afterEach(() => {
    window.fetch = realFetch;
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
    vi.unstubAllEnvs();
    clearCookies();
    document.head.innerHTML = '';
});

describe('window.fetch auth bounce', () => {
    it('sends the user to /login on a 419 from our own API', async () => {
        await boot(() => new Response('{}', { status: 419 }));
        await window.fetch('/api/v1/projects', { method: 'POST' });
        expect(hrefSets).toEqual(['/login?return_to=%2Fprojects%2Fred-star%2Fchat']);
    });

    it.each([401, 419])(
        'never bounces for a %i on the crash-telemetry POST, so the error panel stays up',
        async (status) => {
            await boot(() => new Response('{}', { status }));
            const res = await window.fetch('/api/v1/client-errors', { method: 'POST' });
            expect(res.status).toBe(status);
            expect(hrefSets).toEqual([]);
        },
    );

    it('still leaves sign-in alone', async () => {
        await boot(() => new Response('{}', { status: 419 }));
        await window.fetch('/api/v1/auth/spa-login', { method: 'POST' });
        expect(hrefSets).toEqual([]);
    });

    it('ignores a 401 from another origin', async () => {
        await boot(() => new Response('{}', { status: 401 }));
        await window.fetch('https://tiles.example.com/14/1/2.pbf');
        expect(hrefSets).toEqual([]);
    });
});

/** The slice of Echo's constructor options this file inspects. */
interface CapturedEchoOptions {
    channelAuthorization: {
        transport: string;
        endpoint: string;
        headersProvider: () => Record<string, string>;
    };
}

describe('Echo private-channel authorisation', () => {
    it('reads the CURRENT xsrf cookie on every auth request, not the meta tag Echo freezes at construction', async () => {
        document.head.innerHTML = '<meta name="csrf-token" content="stale-from-page-load">';
        document.cookie = 'XSRF-TOKEN=cookie-1; path=/';
        await boot(() => new Response('{}', { status: 200 }));

        expect(echo.options).toHaveLength(1);
        const auth = (echo.options[0] as CapturedEchoOptions).channelAuthorization;
        expect(auth.transport).toBe('ajax');
        expect(auth.endpoint).toBe('/broadcasting/auth');
        expect(auth.headersProvider()).toEqual({ 'X-XSRF-TOKEN': 'cookie-1' });

        // Sign-out then sign-in (here or in another tab) rotates the token; the
        // next subscription picks the new one up without a reload.
        document.cookie = 'XSRF-TOKEN=cookie-2; path=/';
        expect(auth.headersProvider()).toEqual({ 'X-XSRF-TOKEN': 'cookie-2' });
        expect(auth.headersProvider()).not.toHaveProperty('X-CSRF-TOKEN');
    });
});
