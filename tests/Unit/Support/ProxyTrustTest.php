<?php

declare(strict_types=1);

namespace Tests\Unit\Support;

use App\Support\ProxyTrust;
use Illuminate\Http\Request;
use PHPUnit\Framework\TestCase;

/**
 * Production must not believe X-Forwarded-For.
 *
 * The 2026-08-14 hardening made production fail closed when TRUSTED_PROXIES
 * was UNSET, and the only test asserted the dev '*' behaviour — so nobody
 * noticed that the live Container App set TRUSTED_PROXIES=* outright, and
 * that on Azure Container Apps a client-supplied X-Forwarded-For reaches the
 * app unchanged. $request->ip() was whatever the caller typed: the email+IP
 * login throttle never tripped (a fresh forged IP is a fresh bucket) and the
 * RFC-1918 gate on the unauthenticated /metrics endpoint opened to anyone.
 *
 * The last test here is the one that matters — it drives Symfony's real
 * resolution rather than asserting on a bitmask.
 */
final class ProxyTrustTest extends TestCase
{
    protected function tearDown(): void
    {
        Request::setTrustedProxies([], 0);
        parent::tearDown();
    }

    public function test_production_drops_the_forwarded_for_bit(): void
    {
        $headers = ProxyTrust::forwardedHeaders(true, false);

        $this->assertSame(0, $headers & Request::HEADER_X_FORWARDED_FOR);
    }

    public function test_production_still_trusts_proto_host_and_port(): void
    {
        $headers = ProxyTrust::forwardedHeaders(true, false);

        // isSecure(), and therefore every generated URL and `secure` cookie,
        // depends on Proto surviving.
        $this->assertNotSame(0, $headers & Request::HEADER_X_FORWARDED_PROTO);
        $this->assertNotSame(0, $headers & Request::HEADER_X_FORWARDED_HOST);
        $this->assertNotSame(0, $headers & Request::HEADER_X_FORWARDED_PORT);
    }

    public function test_non_production_keeps_forwarded_for(): void
    {
        $headers = ProxyTrust::forwardedHeaders(false, false);

        $this->assertNotSame(0, $headers & Request::HEADER_X_FORWARDED_FOR);
    }

    public function test_explicit_opt_in_restores_forwarded_for_in_production(): void
    {
        $headers = ProxyTrust::forwardedHeaders(true, 'true');

        $this->assertNotSame(0, $headers & Request::HEADER_X_FORWARDED_FOR);
    }

    public function test_production_fails_closed_when_no_proxy_configured(): void
    {
        $this->assertSame([], ProxyTrust::proxies(null, true));
        $this->assertSame([], ProxyTrust::proxies('', true));
        $this->assertSame([], ProxyTrust::proxies('   ', true));
    }

    public function test_dev_defaults_to_trusting_the_immediate_peer(): void
    {
        $this->assertSame('*', ProxyTrust::proxies(null, false));
    }

    public function test_configured_value_is_passed_through(): void
    {
        $this->assertSame('10.0.0.0/8', ProxyTrust::proxies('10.0.0.0/8', true));
    }

    /** The peer really is a trusted proxy here, so only the mask is in play. */
    private const PEER = '203.0.113.9';

    public function test_forged_forwarded_for_cannot_change_the_client_ip_in_production(): void
    {
        Request::setTrustedProxies([self::PEER], ProxyTrust::forwardedHeaders(true, false));

        $request = Request::create('/metrics', 'GET', server: ['REMOTE_ADDR' => self::PEER]);
        $request->headers->set('X-Forwarded-For', '10.0.0.1');

        // The exact forgery that returned 200 from production /metrics.
        $this->assertSame(self::PEER, $request->ip());
    }

    /**
     * Control: the same request under the pre-fix mask. Without this, the
     * test above would still pass if the header were being ignored for some
     * unrelated reason.
     */
    public function test_forged_forwarded_for_does_win_under_the_dev_mask(): void
    {
        Request::setTrustedProxies([self::PEER], ProxyTrust::forwardedHeaders(false, false));

        $request = Request::create('/metrics', 'GET', server: ['REMOTE_ADDR' => self::PEER]);
        $request->headers->set('X-Forwarded-For', '10.0.0.1');

        $this->assertSame('10.0.0.1', $request->ip());
    }

    public function test_forwarded_proto_still_marks_the_request_secure_in_production(): void
    {
        Request::setTrustedProxies([self::PEER], ProxyTrust::forwardedHeaders(true, false));

        $request = Request::create('/', 'GET', server: ['REMOTE_ADDR' => self::PEER]);
        $request->headers->set('X-Forwarded-Proto', 'https');

        $this->assertTrue($request->isSecure());
    }
}
