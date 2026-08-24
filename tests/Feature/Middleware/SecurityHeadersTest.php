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

    /**
     * The Reports "Original" tab embeds the source PDF from a presigned
     * object-storage URL. Without an explicit frame-src the browser falls
     * back to `default-src 'self'` and refuses it — the tab rendered
     * Chrome's "This content is blocked" panel in every deployment.
     */
    public function test_csp_frame_src_allows_the_configured_object_store(): void
    {
        config(['filesystems.disks.s3-bronze.endpoint' => 'https://acct.blob.core.windows.net']);

        $csp = $this->get('/_test/security-headers/probe')
            ->headers->get('Content-Security-Policy');

        $this->assertMatchesRegularExpression('/(^|; )frame-src /', (string) $csp);
        $this->assertStringContainsString('https://acct.blob.core.windows.net', (string) $csp);
    }

    /**
     * frame-src is what the app may EMBED; frame-ancestors is who may embed
     * the app. Allowing the first must not loosen the second — the two read
     * similarly enough to be conflated in review.
     */
    public function test_allowing_embedded_documents_does_not_allow_framing_the_app(): void
    {
        $resp = $this->get('/_test/security-headers/probe');

        $this->assertStringContainsString(
            "frame-ancestors 'none'",
            (string) $resp->headers->get('Content-Security-Policy'),
        );
        $resp->assertHeader('X-Frame-Options', 'DENY');
    }

    /**
     * An operator pointing storage at their own endpoint must not have to
     * find and edit a CSP allowlist for the document viewer to work.
     */
    public function test_csp_frame_src_follows_a_repointed_storage_endpoint(): void
    {
        config(['filesystems.disks.s3-bronze.endpoint' => 'https://minio.internal:9000']);

        $csp = (string) $this->get('/_test/security-headers/probe')
            ->headers->get('Content-Security-Policy');

        $this->assertStringContainsString('https://minio.internal:9000', $csp);
    }

    /**
     * THE PRODUCTION SHAPE. Under STORAGE_BACKEND=azure_blob every one of
     * these disks resolves to `driver => 'azure'` and takes its host from
     * `account_name`, leaving `endpoint` and `url` unset. Deriving origins
     * from the S3 keys alone therefore produced a frame-src containing only
     * the unconditional s3.amazonaws.com fallback, and the Reports "Original"
     * iframe stayed blocked on Azure even though the directive was present.
     *
     * The presigned host comes from AppServiceProvider's SAS callback:
     * https://{account}.blob.core.windows.net/{container}/{path}?{token}
     */
    public function test_csp_frame_src_allows_the_azure_blob_account(): void
    {
        config([
            'filesystems.disks.s3-bronze.endpoint' => null,
            'filesystems.disks.s3-bronze.url' => null,
            'filesystems.disks.s3-bronze.connection_string' => null,
            'filesystems.disks.s3-bronze.account_name' => 'georagblobcc',
        ]);

        $csp = (string) $this->get('/_test/security-headers/probe')
            ->headers->get('Content-Security-Policy');

        $this->assertStringContainsString('https://georagblobcc.blob.core.windows.net', $csp);
    }

    /**
     * `blob:` in frame-src is the blob: URI SCHEME, not the Azure
     * *.blob.core.windows.net host. Reading one as covering the other is what
     * makes this bug look fixed when it is not, so pin that the real host is
     * present rather than settling for the scheme token.
     */
    public function test_the_blob_scheme_token_is_not_mistaken_for_the_blob_host(): void
    {
        config([
            'filesystems.disks.s3.account_name' => null,
            'filesystems.disks.s3.connection_string' => null,
            'filesystems.disks.s3-bronze.account_name' => null,
            'filesystems.disks.s3-bronze.connection_string' => null,
            'filesystems.disks.s3-exports.account_name' => null,
            'filesystems.disks.s3-exports.connection_string' => null,
        ]);

        $frameSrc = $this->frameSrcDirective(
            (new SecurityHeadersMiddleware)->buildCsp('production'),
        );

        // With no account configured there is no blob host to allow — and the
        // scheme token must not be standing in for one.
        $this->assertStringNotContainsString('blob.core.windows.net', $frameSrc);
    }

    /**
     * A connection string may relocate the host outright — Azurite and
     * private-endpoint deployments both do. BlobEndpoint wins over the
     * account name rather than being ignored beside it.
     */
    public function test_csp_frame_src_honours_a_blob_endpoint_override(): void
    {
        config([
            'filesystems.disks.s3-bronze.account_name' => 'ignoredaccount',
            'filesystems.disks.s3-bronze.connection_string' => 'DefaultEndpointsProtocol=https;AccountName=ignoredaccount;'
                .'BlobEndpoint=https://georag.privatelink.blob.core.windows.net;',
        ]);

        $frameSrc = $this->frameSrcDirective(
            (new SecurityHeadersMiddleware)->buildCsp('production'),
        );

        $this->assertStringContainsString('https://georag.privatelink.blob.core.windows.net', $frameSrc);
        $this->assertStringNotContainsString('ignoredaccount.blob.core.windows.net', $frameSrc);
    }

    /**
     * A sovereign-cloud deployment moves the suffix, not the account.
     */
    public function test_csp_frame_src_follows_a_sovereign_endpoint_suffix(): void
    {
        config([
            'filesystems.disks.s3-bronze.account_name' => 'govacct',
            'filesystems.disks.s3-bronze.connection_string' => 'DefaultEndpointsProtocol=https;AccountName=govacct;EndpointSuffix=core.usgovcloudapi.net;',
        ]);

        $csp = (new SecurityHeadersMiddleware)->buildCsp('production');

        $this->assertStringContainsString('https://govacct.blob.core.usgovcloudapi.net', $csp);
    }

    /**
     * Widening what the app may EMBED must not widen who may embed the app —
     * re-asserted here because the Azure origin is added by a different code
     * path than the S3 one and could regress independently.
     */
    public function test_the_azure_origin_does_not_leak_into_frame_ancestors(): void
    {
        config(['filesystems.disks.s3-bronze.account_name' => 'georagblobcc']);

        $csp = (new SecurityHeadersMiddleware)->buildCsp('production');

        $this->assertStringContainsString("frame-ancestors 'none'", $csp);
        $this->assertStringNotContainsString(
            'georagblobcc',
            $this->directive($csp, 'frame-ancestors'),
        );
    }

    private function frameSrcDirective(string $csp): string
    {
        return $this->directive($csp, 'frame-src');
    }

    private function directive(string $csp, string $name): string
    {
        foreach (explode(';', $csp) as $directive) {
            $directive = trim($directive);
            if (str_starts_with($directive, $name.' ')) {
                return $directive;
            }
        }

        return '';
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

    public function test_csp_allows_every_configured_basemap_origin(): void
    {
        // The basemap URLs are configurable so an air-gapped deployment can
        // point them at its own tile server (CLAUDE.md hard rule #8). That
        // only works if the CSP follows: connect-src used to be four
        // hard-coded hosts, so repointing a basemap produced a style fetch
        // blocked by a policy the operator had no reason to look at.
        config()->set('services.basemap.styles', [
            'positron' => 'https://tiles.internal.example/styles/positron',
            'bright' => 'https://tiles.internal.example/styles/bright',
            'dark_matter' => 'https://tiles.internal.example/styles/dark',
        ]);
        config()->set('services.basemap.glyphs', 'https://fonts.internal.example/{fontstack}/{range}.pbf');
        config()->set('services.basemap.satellite_tiles', 'https://imagery.internal.example/{z}/{y}/{x}');

        $csp = (new SecurityHeadersMiddleware)->buildCsp('production');

        $this->assertStringContainsString('https://tiles.internal.example', $csp);
        $this->assertStringContainsString('https://fonts.internal.example', $csp);
        $this->assertStringContainsString('https://imagery.internal.example', $csp);
        $this->assertStringNotContainsString('openfreemap', $csp);
        $this->assertStringNotContainsString('cartocdn', $csp);
    }

    public function test_csp_allows_sharded_tile_subdomains(): void
    {
        // Carto's dark_matter style.json serves its tiles from
        // a./b./c.basemaps.cartocdn.com. An allowlist holding only the style
        // host loads the style and then blocks every tile it references.
        config()->set('services.basemap.styles', [
            'dark_matter' => 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
        ]);

        $csp = (new SecurityHeadersMiddleware)->buildCsp('production');

        $this->assertStringContainsString('https://basemaps.cartocdn.com', $csp);
        $this->assertStringContainsString('https://*.basemaps.cartocdn.com', $csp);
    }

    public function test_csp_keeps_the_origins_that_are_not_configurable(): void
    {
        // MapLibre's built-in fallback style and the presigned export host
        // are not basemap config, so they cannot be derived.
        $csp = (new SecurityHeadersMiddleware)->buildCsp('production');

        $this->assertStringContainsString('https://demotiles.maplibre.org', $csp);
        $this->assertStringContainsString('https://s3.amazonaws.com', $csp);
    }

    public function test_csp_tolerates_a_relative_basemap_url(): void
    {
        // A deployment serving tiles from its own origin needs no allowlist
        // entry at all — 'self' covers it. Parsing must not emit a broken
        // "://" token that invalidates the whole directive.
        config()->set('services.basemap.styles', ['positron' => '/tiles/positron.json']);
        config()->set('services.basemap.glyphs', null);
        config()->set('services.basemap.satellite_tiles', '');

        $csp = (new SecurityHeadersMiddleware)->buildCsp('production');

        $this->assertStringNotContainsString(' ://', $csp);
        $this->assertStringContainsString("connect-src 'self' wss: ws:", $csp);
    }
}
