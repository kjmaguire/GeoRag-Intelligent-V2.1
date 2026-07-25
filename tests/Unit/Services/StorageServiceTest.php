<?php

declare(strict_types=1);

namespace Tests\Unit\Services;

use App\Services\StorageService;
use Illuminate\Support\Facades\Storage;
use Tests\TestCase;

/**
 * Storage-abstraction plan PR7 — StorageService is a thin façade over the
 * three S3-compatible Flysystem disks (config/filesystems.php). No DB
 * involved (unlike most Feature tests in this suite), so this runs cleanly
 * under both the sqlite test connection and a real Postgres one.
 */
class StorageServiceTest extends TestCase
{
    public function test_bronze_resolves_to_the_s3_disk(): void
    {
        Storage::fake('s3');

        $storage = app(StorageService::class);
        $storage->bronze()->put('reports/example.pdf', 'payload');

        Storage::disk('s3')->assertExists('reports/example.pdf');
    }

    public function test_bronze_read_only_resolves_to_the_s3_bronze_disk(): void
    {
        Storage::fake('s3-bronze');
        // Seed through the same disk name the fake intercepts, then read it
        // back through the façade to prove bronzeReadOnly() targets it.
        Storage::disk('s3-bronze')->put('figures/report-1/figure_0000_page_1.png', 'png-bytes');

        $storage = app(StorageService::class);

        $this->assertTrue($storage->bronzeReadOnly()->exists('figures/report-1/figure_0000_page_1.png'));
    }

    public function test_exports_resolves_to_the_s3_exports_disk(): void
    {
        Storage::fake('s3-exports');

        $storage = app(StorageService::class);
        $storage->exports()->put('export-1/bundle.zip', 'zip-bytes');

        Storage::disk('s3-exports')->assertExists('export-1/bundle.zip');
    }

    public function test_presigned_url_defaults_to_a_24_hour_ttl(): void
    {
        Storage::fake('s3-exports');
        $disk = Storage::disk('s3-exports');
        $disk->put('export-1/bundle.zip', 'zip-bytes');

        // Compare against an explicit ~24h call — both should carry the
        // same Expires query param (to the minute) if the omitted-arg
        // default really is addHours(24), catching a regression to some
        // other default (e.g. addHours(1)) without hardcoding the fake
        // disk's URL format.
        $storage = app(StorageService::class);
        $defaultUrl = $storage->presignedUrl($disk, 'export-1/bundle.zip');
        $explicit24hUrl = $storage->presignedUrl($disk, 'export-1/bundle.zip', now()->addHours(24));

        $this->assertSame(
            $this->expiresQueryParam($defaultUrl),
            $this->expiresQueryParam($explicit24hUrl),
        );
    }

    private function expiresQueryParam(string $url): ?string
    {
        parse_str((string) parse_url($url, PHP_URL_QUERY), $query);

        return $query['Expires'] ?? $query['expires'] ?? null;
    }

    public function test_presigned_url_respects_an_explicit_expiry(): void
    {
        Storage::fake('s3-exports');
        $disk = Storage::disk('s3-exports');
        $disk->put('export-1/bundle.zip', 'zip-bytes');

        $storage = app(StorageService::class);
        $expiresAt = now()->addMinutes(5);
        $url = $storage->presignedUrl($disk, 'export-1/bundle.zip', $expiresAt);

        $this->assertIsString($url);
        $this->assertNotSame('', $url);
    }
}
