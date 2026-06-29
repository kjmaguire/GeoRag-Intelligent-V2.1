import axios from 'axios';
import Echo from 'laravel-echo';
import Pusher from 'pusher-js';

declare global {
    interface Window {
        axios: typeof axios;
        Pusher: typeof Pusher;
        Echo: any;
    }
}

window.axios = axios;
window.axios.defaults.headers.common['X-Requested-With'] = 'XMLHttpRequest';

// ─────────────────────────────────────────────────────────────────────────────
// Global unauthorized handler (window.fetch + axios).
//
// When a Sanctum session expires, components previously saw silent 401/403s
// and rendered bespoke error banners instead of redirecting. This wraps the
// native fetch and registers an axios interceptor so ANY API call that comes
// back with 401/403 flushes the stale token, preserves the user's intended
// URL, and bounces to /login.
//
// Skip conditions:
//   - the response is for the /sanctum or /login endpoints themselves
//     (otherwise we'd infinite-loop during sign-in)
//   - we're already ON /login (no sense redirecting to where we are)
//   - the page is pre-hydration (window/location not available)
// ─────────────────────────────────────────────────────────────────────────────

const AUTH_PATHS = ['/sanctum/csrf-cookie', '/api/v1/auth/login', '/api/v1/auth/spa-login'];

function shouldBounceOnAuthFailure(requestUrl: string | URL | Request): boolean {
    if (typeof window === 'undefined') return false;
    if (window.location.pathname === '/login') return false;

    let url = '';
    if (typeof requestUrl === 'string') url = requestUrl;
    else if (requestUrl instanceof URL) url = requestUrl.pathname;
    else if (requestUrl && typeof (requestUrl as Request).url === 'string') {
        try {
            url = new URL((requestUrl as Request).url, window.location.origin).pathname;
        } catch {
            return false;
        }
    }

    for (const path of AUTH_PATHS) {
        if (url.includes(path)) return false;
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
    const qs = returnTo && returnTo !== '/' && returnTo !== '/login'
        ? `?return_to=${encodeURIComponent(returnTo)}`
        : '';
    window.location.href = `/login${qs}`;
}

if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
    const originalFetch = window.fetch.bind(window);
    window.fetch = async function patchedFetch(
        input: RequestInfo | URL,
        init?: RequestInit,
    ): Promise<Response> {
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

window.axios.interceptors.response.use(
    (response) => response,
    (error) => {
        const status = error?.response?.status;
        const url = error?.config?.url ?? '';
        if ((status === 401 || status === 419) && shouldBounceOnAuthFailure(url)) {
            redirectToLogin();
        }
        return Promise.reject(error);
    },
);

// Laravel Echo + Reverb WebSocket client
// Reverb uses the Pusher protocol but runs on our own server.
window.Pusher = Pusher;

// Use the current page's hostname for the WebSocket connection. This lets
// the same build work from localhost, host.docker.internal, or any other
// hostname — the WS host always matches where the page was loaded from.
window.Echo = new Echo({
    broadcaster: 'reverb',
    key: import.meta.env.VITE_REVERB_APP_KEY,
    wsHost: window.location.hostname,
    wsPort: import.meta.env.VITE_REVERB_PORT ?? 8085,
    wssPort: import.meta.env.VITE_REVERB_PORT ?? 8085,
    // Audit 2026-06-28: follow the PAGE protocol when VITE_REVERB_SCHEME is
    // unset — defaulting to 'http' on an https page yields ws:// and the browser
    // blocks it as mixed content. https page -> wss (forceTLS true).
    forceTLS: (import.meta.env.VITE_REVERB_SCHEME
        ?? (window.location.protocol === 'https:' ? 'https' : 'http')) === 'https',
    enabledTransports: ['ws', 'wss'],
});
