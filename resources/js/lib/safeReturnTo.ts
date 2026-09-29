/**
 * Post-login redirect target from `?return_to=`, or null when it is not a
 * same-origin path.
 *
 * FE-21 (2026-09-29): the check was "starts with / but not //", which lets
 * `/\evil.example` through — WHATWG URL parsing treats `\` as `/` in special
 * schemes, so that resolves to https://evil.example/. The only robust test is
 * to resolve the value exactly as the browser will and compare origins.
 *
 * return_to is only ever produced and consumed client-side (bootstrap.ts's
 * 401 handler → Login.tsx); no server route reads it.
 */
export function safeReturnTo(raw: string | null | undefined, origin: string): string | null {
    if (!raw) return null;
    // Only relative paths are ever produced; refuse anything else outright
    // (absolute URLs, protocol-relative, backslash tricks, control chars).
    if (!raw.startsWith('/') || raw.startsWith('//') || raw.includes('\\')) return null;
    // eslint-disable-next-line no-control-regex
    if (/[\u0000-\u001f\u007f]/.test(raw)) return null;
    let url: URL;
    try {
        url = new URL(raw, origin);
    } catch {
        return null;
    }
    if (url.origin !== origin) return null;
    if (url.pathname === '/login' || url.pathname.startsWith('/login/')) return null;
    return url.pathname + url.search + url.hash;
}
