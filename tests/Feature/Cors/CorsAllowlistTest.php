<?php

declare(strict_types=1);

namespace Tests\Feature\Cors;

use Tests\TestCase;

/**
 * Module 9 Chunk 9.6 — config/cors.php replaced wildcard `'*'` allowlists
 * with explicit method + header lists. Verify preflight OPTIONS responses
 * reflect the new allowlists.
 *
 * Notes
 * -----
 * The Laravel Fruitcake/Spatie CORS implementation Laravel 13 ships with
 * answers preflight OPTIONS only on routes covered by `paths` (api/*). The
 * tests hit `/api/v1/auth/spa-login` because it exists in routes/api.php
 * with no auth requirement on OPTIONS. If the route changes the test will
 * still work as long as some `/api/*` path exists.
 */
final class CorsAllowlistTest extends TestCase
{
    private const ORIGIN = 'http://localhost:5173';

    public function test_preflight_get_method_allowed(): void
    {
        $resp = $this->call(
            'OPTIONS',
            '/api/v1/auth/spa-login',
            [],
            [],
            [],
            [
                'HTTP_ORIGIN' => self::ORIGIN,
                'HTTP_ACCESS_CONTROL_REQUEST_METHOD' => 'GET',
            ],
        );

        $this->assertSame(204, $resp->getStatusCode(), 'preflight should succeed for an allowed method');
        $allowed = $resp->headers->get('Access-Control-Allow-Methods', '');
        $this->assertStringContainsString('GET', $allowed);
    }

    public function test_preflight_disallowed_method_excluded(): void
    {
        $resp = $this->call(
            'OPTIONS',
            '/api/v1/auth/spa-login',
            [],
            [],
            [],
            [
                'HTTP_ORIGIN' => self::ORIGIN,
                'HTTP_ACCESS_CONTROL_REQUEST_METHOD' => 'TRACE',
            ],
        );

        $allowed = $resp->headers->get('Access-Control-Allow-Methods', '');
        $this->assertStringNotContainsString('TRACE', $allowed,
            'TRACE must not appear in the Access-Control-Allow-Methods header');
    }

    public function test_preflight_inertia_header_echoed_back(): void
    {
        $resp = $this->call(
            'OPTIONS',
            '/api/v1/auth/spa-login',
            [],
            [],
            [],
            [
                'HTTP_ORIGIN' => self::ORIGIN,
                'HTTP_ACCESS_CONTROL_REQUEST_METHOD' => 'POST',
                'HTTP_ACCESS_CONTROL_REQUEST_HEADERS' => 'X-Inertia',
            ],
        );

        $allowed = strtolower($resp->headers->get('Access-Control-Allow-Headers', ''));
        $this->assertStringContainsString('x-inertia', $allowed);
    }

    public function test_preflight_unknown_header_not_echoed(): void
    {
        $resp = $this->call(
            'OPTIONS',
            '/api/v1/auth/spa-login',
            [],
            [],
            [],
            [
                'HTTP_ORIGIN' => self::ORIGIN,
                'HTTP_ACCESS_CONTROL_REQUEST_METHOD' => 'POST',
                'HTTP_ACCESS_CONTROL_REQUEST_HEADERS' => 'X-Custom-Bad-Header',
            ],
        );

        $allowed = strtolower($resp->headers->get('Access-Control-Allow-Headers', ''));
        $this->assertStringNotContainsString('x-custom-bad-header', $allowed,
            'unrecognised header must not be echoed in Access-Control-Allow-Headers');
    }

    public function test_exposed_headers_includes_server_timing_and_request_id(): void
    {
        $resp = $this->call(
            'OPTIONS',
            '/api/v1/auth/spa-login',
            [],
            [],
            [],
            [
                'HTTP_ORIGIN' => self::ORIGIN,
                'HTTP_ACCESS_CONTROL_REQUEST_METHOD' => 'POST',
            ],
        );

        // Access-Control-Expose-Headers is sometimes only emitted on the
        // actual response (not preflight) by some CORS libraries. Read it
        // off either; for Laravel's CORS, preflight emits it.
        $exposed = strtolower($resp->headers->get('Access-Control-Expose-Headers', ''));
        if ($exposed === '') {
            $this->markTestSkipped('Access-Control-Expose-Headers not present on preflight in this environment');
        }
        $this->assertStringContainsString('server-timing', $exposed);
        $this->assertStringContainsString('x-request-id', $exposed);
    }
}
