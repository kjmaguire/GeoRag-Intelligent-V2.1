<?php

declare(strict_types=1);

namespace Tests\Feature\Azure;

use App\Services\Azure\AzureBlobDiskLifetime;
use App\Services\Azure\ManagedIdentityTokenProvider;
use App\Services\Azure\RefreshExpiredAzureDisks;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use Laravel\Octane\Events\RequestReceived;
use Tests\TestCase;

/**
 * An Octane worker must not serve a dead managed-identity token forever.
 *
 * ManagedIdentityTokenProvider caches in Redis and refreshes five minutes
 * early, which is right — and was irrelevant, because the only thing that
 * asked it for a token was the `Storage::extend('azure', …)` closure, and
 * that runs once per worker. The token got copied into a BlobRestProxy that
 * has no setter for it, the FilesystemManager cached that disk on a
 * singleton, and `config/octane.php` flushes nothing. So the worker held
 * one token for its whole life and started 401ing on every blob operation
 * the moment it expired.
 *
 * AZURE_STORAGE_AUTH_MODE=managed_identity is set on the live
 * laravel-octane-cc (verified 2026-08-21), so this is the production path,
 * not a latent one.
 */
final class AzureBlobDiskLifetimeTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        AzureBlobDiskLifetime::reset();
        Cache::flush();
    }

    protected function tearDown(): void
    {
        AzureBlobDiskLifetime::reset();
        parent::tearDown();
    }

    // ---------------------------------------------------------------------
    // Expiry bookkeeping
    // ---------------------------------------------------------------------

    public function test_nothing_is_purged_before_a_disk_has_been_built(): void
    {
        $this->assertNull(AzureBlobDiskLifetime::expiresAt());
        $this->assertSame([], AzureBlobDiskLifetime::purgeExpired());
    }

    public function test_a_live_token_is_not_purged(): void
    {
        AzureBlobDiskLifetime::remember(time() + 3600);

        $this->assertSame([], AzureBlobDiskLifetime::purgeExpired());
        $this->assertNotNull(
            AzureBlobDiskLifetime::expiresAt(),
            'A token that has not expired must stay tracked.',
        );
    }

    public function test_an_expired_token_purges_every_azure_disk(): void
    {
        config(['filesystems.disks' => [
            'local' => ['driver' => 'local', 'root' => storage_path()],
            's3' => ['driver' => 'azure', 'container' => 'georag'],
            's3-bronze' => ['driver' => 'azure', 'container' => 'bronze'],
            's3-exports' => ['driver' => 'azure', 'container' => 'exports'],
        ]]);

        AzureBlobDiskLifetime::remember(time() - 1);

        $purged = AzureBlobDiskLifetime::purgeExpired();

        $this->assertEqualsCanonicalizing(
            ['s3', 's3-bronze', 's3-exports'],
            $purged,
            'All three blob disks share one managed identity and one token, '
            .'so they go stale together.',
        );
        $this->assertNotContains('local', $purged);
    }

    public function test_purging_is_idempotent(): void
    {
        config(['filesystems.disks' => [
            's3' => ['driver' => 'azure', 'container' => 'georag'],
        ]]);

        AzureBlobDiskLifetime::remember(time() - 1);

        $this->assertSame(['s3'], AzureBlobDiskLifetime::purgeExpired());
        // Second call must be a no-op, not another round of forgetting —
        // this runs on every request.
        $this->assertSame([], AzureBlobDiskLifetime::purgeExpired());
        $this->assertNull(AzureBlobDiskLifetime::expiresAt());
    }

    public function test_the_earliest_expiry_wins(): void
    {
        $soon = time() + 60;
        AzureBlobDiskLifetime::remember(time() + 3600);
        AzureBlobDiskLifetime::remember($soon);
        AzureBlobDiskLifetime::remember(time() + 7200);

        $this->assertSame($soon, AzureBlobDiskLifetime::expiresAt());
    }

    public function test_disk_discovery_reads_config_rather_than_a_hardcoded_list(): void
    {
        // A fourth blob disk must not silently keep a dead token.
        config(['filesystems.disks' => [
            's3' => ['driver' => 'azure', 'container' => 'georag'],
            's3-archive' => ['driver' => 'azure', 'container' => 'archive'],
            'public' => ['driver' => 'local', 'root' => storage_path()],
        ]]);

        $this->assertEqualsCanonicalizing(
            ['s3', 's3-archive'],
            AzureBlobDiskLifetime::azureDiskNames(),
        );
    }

    // ---------------------------------------------------------------------
    // The listener
    // ---------------------------------------------------------------------

    public function test_the_octane_listener_is_registered_on_request_received(): void
    {
        $listeners = config('octane.listeners.'.RequestReceived::class);

        $this->assertContains(
            RefreshExpiredAzureDisks::class,
            $listeners,
            'Without this listener nothing ever notices the token expired. '
            .'A cached disk is only rebuilt when something forgets it, and '
            .'config/octane.php flushes no bindings.',
        );
    }

    public function test_the_listener_purges_when_invoked(): void
    {
        config(['filesystems.disks' => [
            's3' => ['driver' => 'azure', 'container' => 'georag'],
        ]]);
        AzureBlobDiskLifetime::remember(time() - 1);

        (new RefreshExpiredAzureDisks)->handle(new \stdClass);

        $this->assertNull(AzureBlobDiskLifetime::expiresAt());
    }

    // ---------------------------------------------------------------------
    // The token provider's half of the contract
    // ---------------------------------------------------------------------

    public function test_the_provider_returns_an_expiry_alongside_the_token(): void
    {
        putenv('IDENTITY_ENDPOINT=http://localhost:42/msi/token');
        putenv('IDENTITY_HEADER=header-value');
        $_ENV['IDENTITY_ENDPOINT'] = 'http://localhost:42/msi/token';
        $_ENV['IDENTITY_HEADER'] = 'header-value';

        $expiresOn = time() + 86400;
        Http::fake([
            '*' => Http::response([
                'access_token' => 'a-bearer-token',
                'expires_on' => (string) $expiresOn,
            ], 200),
        ]);

        try {
            [$token, $expiresAt] = (new ManagedIdentityTokenProvider)->getTokenWithExpiry();

            $this->assertSame('a-bearer-token', $token);
            // The five-minute safety margin is already applied, so the
            // caller can treat this as "rebuild at or after here" without
            // redoing the arithmetic.
            $this->assertLessThan($expiresOn, $expiresAt);
            $this->assertGreaterThan($expiresOn - 400, $expiresAt);
        } finally {
            putenv('IDENTITY_ENDPOINT');
            putenv('IDENTITY_HEADER');
            unset($_ENV['IDENTITY_ENDPOINT'], $_ENV['IDENTITY_HEADER']);
        }
    }

    public function test_get_token_still_returns_just_the_string(): void
    {
        // Existing callers must not have to change.
        putenv('IDENTITY_ENDPOINT=http://localhost:42/msi/token');
        putenv('IDENTITY_HEADER=header-value');
        $_ENV['IDENTITY_ENDPOINT'] = 'http://localhost:42/msi/token';
        $_ENV['IDENTITY_HEADER'] = 'header-value';

        Http::fake([
            '*' => Http::response([
                'access_token' => 'a-bearer-token',
                'expires_on' => (string) (time() + 86400),
            ], 200),
        ]);

        try {
            $this->assertSame('a-bearer-token', (new ManagedIdentityTokenProvider)->getToken());
        } finally {
            putenv('IDENTITY_ENDPOINT');
            putenv('IDENTITY_HEADER');
            unset($_ENV['IDENTITY_ENDPOINT'], $_ENV['IDENTITY_HEADER']);
        }
    }

    public function test_a_cached_token_without_its_expiry_is_refetched(): void
    {
        // The two cache keys share a TTL, so this should not happen — but if
        // it ever does, returning a token with no known expiry would put us
        // straight back to holding it forever.
        Cache::put('azure:msi_token:storage', 'stale-token', 3600);
        // Deliberately no expiry key.

        putenv('IDENTITY_ENDPOINT=http://localhost:42/msi/token');
        putenv('IDENTITY_HEADER=header-value');
        $_ENV['IDENTITY_ENDPOINT'] = 'http://localhost:42/msi/token';
        $_ENV['IDENTITY_HEADER'] = 'header-value';

        Http::fake([
            '*' => Http::response([
                'access_token' => 'fresh-token',
                'expires_on' => (string) (time() + 86400),
            ], 200),
        ]);

        try {
            [$token, $expiresAt] = (new ManagedIdentityTokenProvider)->getTokenWithExpiry();
            $this->assertSame('fresh-token', $token);
            $this->assertGreaterThan(time(), $expiresAt);
        } finally {
            putenv('IDENTITY_ENDPOINT');
            putenv('IDENTITY_HEADER');
            unset($_ENV['IDENTITY_ENDPOINT'], $_ENV['IDENTITY_HEADER']);
        }
    }
}
