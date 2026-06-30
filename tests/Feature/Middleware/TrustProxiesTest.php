<?php

declare(strict_types=1);

namespace Tests\Feature\Middleware;

use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;
use Tests\TestCase;

/**
 * Module 9 Chunk 9.5 — verify the bootstrap/app.php $middleware->trustProxies
 * registration honours X-Forwarded-* headers from any upstream (TRUSTED_PROXIES
 * defaults to '*' for dev). Production overrides the env var with an explicit
 * CIDR allowlist.
 *
 * Without trust the auth-login rate limiter (which keys on email + IP per
 * routes/api.php:38-44) collapses to a single bucket behind nginx/Traefik.
 */
final class TrustProxiesTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        Route::get('/_test/proxy/echo', fn (Request $r) => [
            'ip' => $r->ip(),
            'is_secure' => $r->isSecure(),
            'host' => $r->getHost(),
        ]);
    }

    public function test_x_forwarded_for_overrides_remote_addr_when_proxy_trusted(): void
    {
        // The default TRUSTED_PROXIES='*' means any caller can claim to be a proxy.
        $resp = $this->withHeaders([
            'X-Forwarded-For' => '203.0.113.42',
        ])->get('/_test/proxy/echo');

        $resp->assertOk();
        $this->assertSame('203.0.113.42', $resp->json('ip'));
    }

    public function test_x_forwarded_proto_https_marks_request_as_secure(): void
    {
        $resp = $this->withHeaders([
            'X-Forwarded-Proto' => 'https',
        ])->get('/_test/proxy/echo');

        $resp->assertOk();
        $this->assertTrue($resp->json('is_secure'));
    }

    public function test_x_forwarded_host_overrides_host_when_proxy_trusted(): void
    {
        $resp = $this->withHeaders([
            'X-Forwarded-Host' => 'georag.example.com',
        ])->get('/_test/proxy/echo');

        $resp->assertOk();
        $this->assertSame('georag.example.com', $resp->json('host'));
    }
}
