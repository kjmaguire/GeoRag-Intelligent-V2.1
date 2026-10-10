import Echo from 'laravel-echo';
import Pusher from 'pusher-js';

declare global {
    interface Window {
        Pusher: typeof Pusher;
        Echo: Echo<'reverb'>;
    }
}

// Axios used to be imported here, exposed as a window global and given a 401/419
// interceptor, and nothing ever called it — Inertia v3 dropped Axios for its
// own XHR client, and the app's own calls use fetch (patched below). It sat
// in the main chunk for nothing (FE-23).

// ─────────────────────────────────────────────────────────────────────────────
// Global unauthorized handler (window.fetch).
//
// When a Sanctum session expires, components previously saw silent 401/403s
// and rendered bespoke error banners instead of redirecting. This wraps the
// native fetch so ANY API call that comes back with 401/419 flushes the
// stale token, preserves the user's intended URL, and bounces to /login.
//
// Skip conditions:
//   - the response is for the /sanctum or /login endpoints themselves
//     (otherwise we'd infinite-loop during sign-in)
//   - the response is for the crash-telemetry POST. The ErrorBoundary sends it
//     while it is showing "Something went wrong"; a 401/419 there (an expired
//     session, a stale token) must not navigate away and replace that panel
//     with the sign-in page — the report is best-effort and its failure says
//     nothing the reader needs to act on
//   - we're already ON /login (no sense redirecting to where we are)
//   - the page is pre-hydration (window/location not available)
//   - the request is cross-origin (a 401 from another host is not our session)
// ─────────────────────────────────────────────────────────────────────────────

const NO_BOUNCE_PATHS = [
    '/sanctum/csrf-cookie',
    '/api/v1/auth/login',
    '/api/v1/auth/spa-login',
    '/api/v1/client-errors',
];

function shouldBounceOnAuthFailure(requestUrl: string | URL | Request): boolean {
    if (typeof window === 'undefined') return false;
    if (window.location.pathname === '/login') return false;

    // Only OUR session expiring should bounce the user to /login. A 401 from
    // a third-party host (a tile server, an external API) says nothing about
    // the Sanctum session, so resolve against the page origin and ignore
    // anything cross-origin.
    let parsed: URL;
    try {
        const raw =
            typeof requestUrl === 'string'
                ? requestUrl
                : requestUrl instanceof URL
                  ? requestUrl.href
                  : (requestUrl as Request).url;
        parsed = new URL(raw, window.location.origin);
    } catch {
        return false;
    }
    if (parsed.origin !== window.location.origin) return false;

    for (const path of NO_BOUNCE_PATHS) {
        if (parsed.pathname.includes(path)) return false;
    }
    return true;
}

function redirectToLogin(): void {
    if (typeof window === 'undefined') return;
    try {
        localStorage.removeItem('georag_token');
        localStorage.removeItem('georag_user');
    } catch {
        /* storage disabled is fine */
    }
    const returnTo = window.location.pathname + window.location.search;
    const qs =
        returnTo && returnTo !== '/' && returnTo !== '/login' ? `?return_to=${encodeURIComponent(returnTo)}` : '';
    window.location.href = `/login${qs}`;
}

if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
    const originalFetch = window.fetch.bind(window);
    window.fetch = async function patchedFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
        const response = await originalFetch(input, init);
        if (
            (response.status === 401 || response.status === 419) &&
            shouldBounceOnAuthFailure(input as string | URL | Request)
        ) {
            redirectToLogin();
        }
        return response;
    };
}

// Laravel Echo + Reverb WebSocket client
// Reverb uses the Pusher protocol but runs on our own server.
window.Pusher = Pusher;

// WS host resolution: honour VITE_REVERB_HOST when the build provides one —
// where Reverb is served from a DIFFERENT hostname than the page, deriving
// wsHost from window.location silently dials an endpoint that doesn't speak
// WebSocket and every chat stream/progress event is lost. Falls back to the
// page hostname for localhost / docker-compose builds, where that is correct.
const reverbScheme: string =
    import.meta.env.VITE_REVERB_SCHEME ?? (window.location.protocol === 'https:' ? 'https' : 'http');
const reverbHost: string = import.meta.env.VITE_REVERB_HOST || window.location.hostname;
// Port default follows the scheme: TLS deployments terminate on 443;
// plain-http local stacks keep the legacy 8085 Reverb port.
const reverbPort: number = Number(import.meta.env.VITE_REVERB_PORT || (reverbScheme === 'https' ? 443 : 8085));
if (!import.meta.env.VITE_REVERB_APP_KEY) {
    console.error('GeoRAG: VITE_REVERB_APP_KEY is not set in this build, so live chat updates cannot connect.');
}
window.Echo = new Echo({
    broadcaster: 'reverb',
    key: import.meta.env.VITE_REVERB_APP_KEY,
    wsHost: reverbHost,
    wsPort: reverbPort,
    wssPort: reverbPort,
    // Audit 2026-06-28: follow the PAGE protocol when VITE_REVERB_SCHEME is
    // unset — defaulting to 'http' on an https page yields ws:// and the browser
    // blocks it as mixed content. https page -> wss (forceTLS true).
    forceTLS: reverbScheme === 'https',
    enabledTransports: ['ws', 'wss'],
});
