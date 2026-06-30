<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use App\Services\FastApiHttpClient;
use Closure;
use Illuminate\Http\Request;
use Symfony\Component\HttpFoundation\Response;

/**
 * Module 10 Chunk 10.6 — W3C Trace Context propagation.
 *
 * Reads or mints a `traceparent` header on every inbound request, exposes it
 * via {@see Request::attributes()} so downstream code (jobs, internal HTTP
 * calls, log writers) can reuse the same trace_id, and re-emits the header
 * on the response so callers can correlate.
 *
 * W3C Trace Context spec (https://www.w3.org/TR/trace-context/):
 *
 *   traceparent: 00-<trace-id>-<parent-id>-<flags>
 *
 *     - version    "00" (2 hex chars)
 *     - trace-id   16 bytes (32 hex chars)
 *     - parent-id  8  bytes (16 hex chars)
 *     - flags      "01" (sampled) or "00" (not sampled) — we always sample.
 *
 * Octane-safe: no per-instance state. The middleware reads attributes from
 * the request object and sets a single response header. Storage of the
 * trace-id for the duration of the request happens via the request itself.
 *
 * Forwarding policy: when Laravel makes an internal HTTP call to FastAPI
 * (see {@see FastApiHttpClient} or wherever the outbound
 * client lives), the caller pulls the trace-id from
 * `$request->attributes->get('traceparent')` and includes it as a header on
 * the outbound request. This is application code, not middleware — the
 * middleware only handles the inbound side.
 */
final class InjectTraceparent
{
    public const ATTRIBUTE_KEY = 'traceparent';

    public const HEADER_NAME = 'traceparent';

    public function handle(Request $request, Closure $next): Response
    {
        $traceparent = $request->headers->get(self::HEADER_NAME);

        if (! self::isValid($traceparent)) {
            $traceparent = self::mint();
        }

        $request->attributes->set(self::ATTRIBUTE_KEY, $traceparent);

        /** @var Response $response */
        $response = $next($request);
        $response->headers->set(self::HEADER_NAME, $traceparent);

        return $response;
    }

    /**
     * Validate a traceparent string per W3C v00. Reject anything that doesn't
     * exactly match the version-traceid-parentid-flags shape.
     */
    public static function isValid(?string $traceparent): bool
    {
        if ($traceparent === null) {
            return false;
        }

        // 00-<32 hex>-<16 hex>-<2 hex>
        return (bool) preg_match(
            '/^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/',
            $traceparent,
        );
    }

    /**
     * Mint a fresh traceparent. random_bytes is cryptographically strong;
     * the trace-id and parent-id are independent so an attacker can't
     * predict either from the other.
     */
    public static function mint(): string
    {
        return sprintf(
            '00-%s-%s-01',
            bin2hex(random_bytes(16)),  // 16-byte trace-id
            bin2hex(random_bytes(8)),   // 8-byte parent-id
        );
    }

    /**
     * Convenience: extract the 32-hex trace_id portion of a valid header.
     * Returns null if the header is malformed.
     */
    public static function traceIdOf(?string $traceparent): ?string
    {
        if (! self::isValid($traceparent)) {
            return null;
        }

        return substr((string) $traceparent, 3, 32);
    }
}
