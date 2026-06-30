<?php

declare(strict_types=1);

namespace Tests\Feature\Middleware;

use App\Http\Middleware\SecurityHeadersMiddleware;
use Illuminate\Auth\Middleware\Authenticate;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;
use Tests\TestCase;

/**
 * Module 9 Chunk 9.5 — verify SecurityHeadersMiddleware emits the expected
 * always-on header set, plus HSTS only when the request is HTTPS, plus a
 * CSP that includes the directives the audit asked for.
 *
 * The middleware is registered globally in bootstrap/app.php so we test
 * via a regular route hit rather than instantiating the middleware
 * directly — that way we also catch a regression where the middleware
 * wasn't registered.
 */
final class SecurityHeadersTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        Route::get('/_test/security-headers/probe', fn () => 'ok')->withoutMiddleware([
            // Prevent any auth guards from interfering with the probe route.
            Authenticate::class,
        ]);
    }

    public function test_always_on_headers_present_on_response(): void
    {
        $resp = $this->get('/_test/security-headers/probe');

        $resp->assertOk();
        $resp->assertHeader('X-Frame-Options', 'DENY');
        $resp->assertHeader('X-Content-Type-Options', 'nosniff');
        $resp->assertHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
        $this->assertSame(
            'geolocation=(), microphone=(), camera=(), payment=()',
            $resp->headers->get('Permissions-Policy'),
        );
    }

    public function test_csp_present_on_response(): void
    {
        $resp = $this->get('/_test/security-headers/probe');

        $csp = $resp->headers->get('Content-Security-Policy');
        $this->assertIsString($csp);
        $this->assertStringContainsString("default-src 'self'", $csp);
        $this->assertStringContainsString("frame-ancestors 'none'", $csp);
        $this->assertStringContainsString("object-src 'none'", $csp);
    }

    public function test_csp_connect_src_includes_websocket_schemes(): void
    {
        $resp = $this->get('/_test/security-headers/probe');
        $csp = $resp->headers->get('Content-Security-Policy');

        $this->assertStringContainsString("connect-src 'self' wss: ws:", $csp);
    }

    public function test_csp_worker_src_allows_blob_for_maplibre(): void
    {
        $resp = $this->get('/_test/security-headers/probe');
        $csp = $resp->headers->get('Content-Security-Policy');

        $this->assertStringContainsString("worker-src 'self' blob:", $csp);
    }

    public function test_hsts_absent_on_http_request(): void
    {
        $resp = $this->get('/_test/security-headers/probe');
        $resp->assertHeaderMissing('Strict-Transport-Security');
    }

    public function test_hsts_present_on_https_request(): void
    {
        // Laravel's TestCase doesn't honour `withServerVariables(['HTTPS'=>'on'])`
        // through to Symfony's Request::isSecure() reliably. Instead, exercise
        // the middleware directly with a Symfony Request whose scheme is https.
        $request = Request::create('https://georag.example.com/_test/security-headers/probe', 'GET');
        $this->assertTrue($request->isSecure(), 'precondition: scheme=https should be secure');

        $mw = new SecurityHeadersMiddleware;
        $resp = $mw->handle($request, fn ($r) => response('ok'));

        $hsts = $resp->headers->get('Strict-Transport-Security');
        $this->assertIsString($hsts);
        $this->assertStringContainsString('max-age=31536000', $hsts);
        $this->assertStringContainsString('includeSubDomains', $hsts);
    }

    public function test_csp_omits_upgrade_insecure_in_local_env(): void
    {
        // Default test env is `testing`. Build the CSP directly with env=local
        // to verify the conditional logic — this also exercises the helper.
        $mw = new SecurityHeadersMiddleware;
        $csp_local = $mw->buildCsp('local');
        $this->assertStringNotContainsString('upgrade-insecure-requests', $csp_local);
    }

    public function test_csp_includes_upgrade_insecure_in_production(): void
    {
        $mw = new SecurityHeadersMiddleware;
        $csp_prod = $mw->buildCsp('production');
        $this->assertStringContainsString('upgrade-insecure-requests', $csp_prod);
    }
}
