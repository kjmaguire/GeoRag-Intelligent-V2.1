<?php

declare(strict_types=1);

namespace Tests\Unit\Services;

use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Storage;
use Tests\TestCase;

/**
 * Covers the STORAGE_BACKEND=azure_blob disk driver registered in
 * AppServiceProvider::boot() (Storage::extend('azure', ...)). Unlike
 * StorageServiceTest, this does NOT use Storage::fake() — the fake swaps out
 * the disk entirely and would never exercise the real 'azure' driver
 * registration or its temporaryUrl() SAS callback, which is exactly the code
 * path that broke live: AzureBlobStorageAdapter implements neither
 * getTemporaryUrl() nor Flysystem's TemporaryUrlGenerator, so without the
 * buildTemporaryUrlsUsing() callback every presigned-download call site
 * (report/figure exports, GenerateExportJob, FigureResolver) threw
 * "This driver does not support creating temporary URLs."
 *
 * The fake account key below is well-formed base64 but not a real Azure
 * secret — SAS token generation is pure local HMAC signing with no network
 * call, so this runs fully offline.
 */
class AzureBlobDiskTest extends TestCase
{
    private function configureAzureDisk(): void
    {
        config([
            'filesystems.disks.s3.driver' => 'azure',
            'filesystems.disks.s3.connection_string' => 'DefaultEndpointsProtocol=https;'
                .'AccountName=georagteststorage;'
                .'AccountKey='.base64_encode('not-a-real-key-just-test-fixture-bytes').';'
                .'EndpointSuffix=core.windows.net',
            'filesystems.disks.s3.container' => 'bronze',
        ]);
    }

    public function test_azure_disk_resolves_without_throwing(): void
    {
        $this->configureAzureDisk();

        $disk = Storage::disk('s3');

        $this->assertSame('azure', config('filesystems.disks.s3.driver'));
        // getAdapter() only exists on the underlying Flysystem-backed
        // FilesystemAdapter — resolving it at all proves Storage::extend's
        // closure ran without throwing (e.g. on a malformed connection
        // string or a missing SDK class).
        $this->assertNotNull($disk->getAdapter());
    }

    public function test_azure_disk_provides_a_temporary_url_instead_of_throwing(): void
    {
        $this->configureAzureDisk();

        $disk = Storage::disk('s3');

        $this->assertTrue(
            $disk->providesTemporaryUrls(),
            'buildTemporaryUrlsUsing() callback did not register — '
            .'temporaryUrl() would throw "This driver does not support '
            .'creating temporary URLs" for every report/figure export.',
        );

        $url = $disk->temporaryUrl('reports/example.pdf', now()->addHours(24));

        $this->assertStringStartsWith(
            'https://georagteststorage.blob.core.windows.net/bronze/reports/example.pdf?',
            $url,
        );
        // A real SAS token carries a signature param — proves this is an
        // actual signed URL, not just a bare path with no auth.
        $this->assertStringContainsString('sig=', $url);
    }

    public function test_azure_disk_temporary_url_scopes_to_the_requested_path(): void
    {
        $this->configureAzureDisk();

        $disk = Storage::disk('s3');

        $urlA = $disk->temporaryUrl('reports/a.pdf', now()->addHour());
        $urlB = $disk->temporaryUrl('reports/b.pdf', now()->addHour());

        $this->assertStringContainsString('reports/a.pdf', $urlA);
        $this->assertStringContainsString('reports/b.pdf', $urlB);
        // Different resource paths must produce different signatures —
        // a bug that hardcoded the signed resource would let A's SAS
        // token be reused to read B.
        $this->assertNotSame(
            explode('sig=', $urlA)[1] ?? null,
            explode('sig=', $urlB)[1] ?? null,
        );
    }

    /**
     * Managed-identity mode — the blob CLIENT authenticates via a
     * Container-Apps-injected managed-identity token (mocked here, no real
     * network call), NOT the account key. The account key stays configured
     * only because temporaryUrl() SAS signing has no user-delegation-key
     * equivalent in this SDK version (see AppServiceProvider's comment on
     * the same closure).
     */
    private function fakeIdentityEndpoint(): void
    {
        putenv('IDENTITY_ENDPOINT=http://localhost:12356/msi/token');
        putenv('IDENTITY_HEADER=fake-identity-header-secret');
        Cache::forget('azure:msi_token:storage');
        Http::fake([
            'http://localhost:12356/*' => Http::response([
                'access_token' => 'fake-msi-token',
                'expires_on' => (string) (time() + 3600),
            ], 200),
        ]);
    }

    public function test_azure_disk_resolves_in_managed_identity_mode_without_throwing(): void
    {
        $this->fakeIdentityEndpoint();

        $this->configureAzureDisk();
        config([
            'filesystems.disks.s3.auth_mode' => 'managed_identity',
            'filesystems.disks.s3.account_name' => 'georagteststorage',
        ]);

        $disk = Storage::disk('s3');

        $this->assertNotNull($disk->getAdapter());
        Http::assertSent(fn ($request) => str_contains($request->url(), 'localhost:12356'));
    }

    public function test_azure_disk_managed_identity_mode_still_provides_temporary_urls(): void
    {
        $this->fakeIdentityEndpoint();

        $this->configureAzureDisk();
        config([
            'filesystems.disks.s3.auth_mode' => 'managed_identity',
            'filesystems.disks.s3.account_name' => 'georagteststorage',
        ]);

        $disk = Storage::disk('s3');

        // SAS signing still runs off the (deliberately still-configured)
        // AccountKey — see the SDK-limitation comment in AppServiceProvider.
        $this->assertTrue($disk->providesTemporaryUrls());
        $url = $disk->temporaryUrl('reports/example.pdf', now()->addHours(24));
        $this->assertStringContainsString('sig=', $url);
    }

    protected function tearDown(): void
    {
        putenv('IDENTITY_ENDPOINT');
        putenv('IDENTITY_HEADER');
        parent::tearDown();
    }
}
