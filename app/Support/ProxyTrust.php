<?php

declare(strict_types=1);

namespace App\Support;

use Illuminate\Http\Request;

/**
 * What the application is willing to believe from the reverse proxy.
 *
 * Lives here rather than inline in bootstrap/app.php so the production
 * posture is testable: the bootstrap closure runs once per process, and
 * TrustProxies stores its configuration in static properties, so a test
 * cannot rebuild the app under a different APP_ENV without leaking that
 * state into every test after it.
 *
 * Pure functions of the environment. No state, so Octane-safe.
 */
final class ProxyTrust
{
    /**
     * Which proxies to trust.
     *
     * Production fails closed when nothing is configured: with no trusted
     * proxy, no X-Forwarded-* header is honoured at all. '*' is the
     * dev-friendly default everywhere else — in Laravel it means "trust the
     * immediate peer", not "trust anything".
     *
     * @return array<int, string>|string
     */
    public static function proxies(?string $configured, bool $isProduction): array|string
    {
        if ($configured === null || trim($configured) === '') {
            return $isProduction ? [] : '*';
        }

        return $configured;
    }

    /**
     * Which X-Forwarded-* headers to believe.
     *
     * Host/Port/Proto always: the ingress sets them, and isSecure() (and
     * therefore every generated URL and every `secure` cookie) depends on
     * Proto.
     *
     * For is the exception, and only in production. Azure Container Apps
     * ingress passes a client-supplied X-Forwarded-For straight through
     * instead of appending the peer address to the chain. Symfony resolves
     * the client IP by walking the chain from the right and discarding
     * trusted hops, so when the whole chain is client-written there is no
     * trusted-proxy setting that recovers the truth — $request->ip() is
     * simply whatever the caller typed. Measured on 2026-08-20: GET /metrics
     * returned 403 with no header, 200 with `X-Forwarded-For: 10.0.0.1`, and
     * 403 with `X-Forwarded-For: 8.8.8.8`.
     *
     * Dropping the FOR bit makes ip() the immediate peer: identical for
     * every caller, and unforgeable. Nothing depended on the finer grain —
     * the login limiter also keys on the email, so it stays 5/min per
     * account, and the query limiter keys on the authenticated user id.
     *
     * Set TRUST_FORWARDED_FOR=true only behind an ingress that APPENDS to
     * the chain (Application Gateway, Front Door, a correctly configured
     * nginx), and re-run those three requests before believing it.
     */
    public static function forwardedHeaders(bool $isProduction, mixed $trustForwardedFor): int
    {
        $headers = Request::HEADER_X_FORWARDED_FOR
            | Request::HEADER_X_FORWARDED_HOST
            | Request::HEADER_X_FORWARDED_PORT
            | Request::HEADER_X_FORWARDED_PROTO;

        if ($isProduction && ! filter_var($trustForwardedFor, FILTER_VALIDATE_BOOL)) {
            $headers &= ~Request::HEADER_X_FORWARDED_FOR;
        }

        return $headers;
    }
}
