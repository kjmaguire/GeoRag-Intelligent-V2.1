/**
 * CSRF for same-origin `fetch()` calls.
 *
 * Laravel answers a state-changing request from a stateful (first-party)
 * client with 419 unless it carries the session's CSRF token, and accepts it
 * in two headers: `X-CSRF-TOKEN` and `X-XSRF-TOKEN`. When BOTH are present
 * the first one wins, even if it is stale and the second is current.
 *
 * `<meta name="csrf-token">` is rendered once, by the blade layout, on a FULL
 * page load. This app navigates with Inertia (XHR), so after a sign-out and a
 * sign-in in the same tab — or one done in another tab — the session's token
 * has moved on and the meta tag has not: every call that read it sent a dead
 * token, got a 419, and the patched `window.fetch` (bootstrap.ts) took that for
 * an expired session and bounced to /login. A second "Sign out" in that tab
 * failed the same way, silently.
 *
 * The `XSRF-TOKEN` cookie is rewritten by Laravel on every stateful response,
 * so it is always current. It is also exactly what Inertia's own XHR client
 * sends. Read it here, and send it as `X-XSRF-TOKEN` — never `X-CSRF-TOKEN`.
 */

const XSRF_COOKIE = /(?:^|;\s*)XSRF-TOKEN=([^;]*)/;

/** The current CSRF token from the XSRF-TOKEN cookie, decoded; null when there is none. */
export function xsrfToken(): string | null {
    if (typeof document === 'undefined') return null;
    const match = document.cookie.match(XSRF_COOKIE);
    if (!match) return null;
    try {
        return decodeURIComponent(match[1]);
    } catch {
        // A malformed escape: treat the cookie as absent rather than throw
        // from inside an unrelated request.
        return null;
    }
}

/**
 * Headers that satisfy Laravel's CSRF check for a same-origin request —
 * spread them into `fetch` headers. Empty when no token is available (the
 * request then fails with a 419 the caller can show; sending a guess would
 * only hide why).
 */
export function csrfHeaders(): Record<string, string> {
    const token = xsrfToken();
    return token ? { 'X-XSRF-TOKEN': token } : {};
}
