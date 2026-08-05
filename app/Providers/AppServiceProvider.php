<?php

declare(strict_types=1);

namespace App\Providers;

use App\Models\User;
use App\Policies\DashboardPolicy;
use App\Support\Http\PooledHttpClient;
use Illuminate\Cache\RateLimiting\Limit;
use Illuminate\Filesystem\FilesystemAdapter as LaravelFilesystemAdapter;
use Illuminate\Http\Client\Factory as HttpFactory;
use Illuminate\Http\Request;
use Illuminate\Log\Events\MessageLogged;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Gate;
use Illuminate\Support\Facades\RateLimiter;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\ServiceProvider;
use League\Flysystem\AzureBlobStorage\AzureBlobStorageAdapter;
use League\Flysystem\Filesystem as Flysystem;
use MicrosoftAzure\Storage\Blob\BlobRestProxy;
use MicrosoftAzure\Storage\Blob\BlobSharedAccessSignatureHelper;
use MicrosoftAzure\Storage\Blob\Internal\BlobResources;

class AppServiceProvider extends ServiceProvider
{
    /**
     * Register any application services.
     */
    public function register(): void
    {
        // PooledHttpClient — Guzzle client pool with TCP keep-alive per base
        // URL. Survives between requests in the same Octane worker so curl
        // sockets stay open to FastAPI and other internal services. State is bounded
        // (≤16 base URLs, LRU eviction); no per-request data is retained.
        // See app/Support/Http/PooledHttpClient.php for the Octane-safety note.
        $this->app->singleton(PooledHttpClient::class, fn ($app) => new PooledHttpClient(
            $app->make(HttpFactory::class),
        ));
    }

    /**
     * Bootstrap any application services.
     */
    public function boot(): void
    {
        // Azure Blob disk driver — mirrors STORAGE_BACKEND=azure_blob on the
        // Python side (georag_object_storage/factory.py). Registered
        // unconditionally; it's only instantiated when a disk config actually
        // resolves 'driver' => 'azure' (see config/filesystems.php).
        Storage::extend('azure', function ($app, $config) {
            $client = BlobRestProxy::createBlobService($config['connection_string']);
            $adapter = new AzureBlobStorageAdapter($client, $config['container']);

            $disk = new LaravelFilesystemAdapter(new Flysystem($adapter), $adapter, $config);

            // AzureBlobStorageAdapter implements neither getTemporaryUrl() nor
            // League's TemporaryUrlGenerator interface, so temporaryUrl() would
            // otherwise throw "This driver does not support creating temporary
            // URLs" — breaking every presigned-download call site (report/
            // figure exports, GenerateExportJob, FigureResolver). Register a
            // SAS-token callback so it behaves the same as the s3 disks it
            // replaces under STORAGE_BACKEND=azure_blob.
            if (str_contains((string) $config['connection_string'], 'AccountName=')) {
                preg_match('/AccountName=([^;]+)/', (string) $config['connection_string'], $nameMatch);
                preg_match('/AccountKey=([^;]+)/', (string) $config['connection_string'], $keyMatch);
                $accountName = $nameMatch[1] ?? null;
                $accountKey = $keyMatch[1] ?? null;

                if ($accountName && $accountKey) {
                    $sasHelper = new BlobSharedAccessSignatureHelper($accountName, $accountKey);
                    $container = $config['container'];

                    $disk->buildTemporaryUrlsUsing(
                        function (string $path, \DateTimeInterface $expiration, array $options = []) use (
                            $sasHelper, $accountName, $container
                        ): string {
                            $token = $sasHelper->generateBlobServiceSharedAccessSignatureToken(
                                BlobResources::RESOURCE_TYPE_BLOB,
                                "{$container}/{$path}",
                                'r',
                                $expiration,
                            );

                            return "https://{$accountName}.blob.core.windows.net/{$container}/{$path}?{$token}";
                        },
                    );
                }
            }

            return $disk;
        });

        Gate::define('viewPortfolio', [DashboardPolicy::class, 'viewPortfolio']);
        Gate::define('viewProject', [DashboardPolicy::class, 'viewProject']);

        // Global admin gate — guards write access to shared resources such as
        // vendor profiles and column mappings. Reads directly from the users
        // table column; no role package is required at this scale.
        Gate::define('admin', fn (User $user): bool => (bool) $user->is_admin);

        // ── Rate limiters ────────────────────────────────────────────
        //
        // auth-login: 5 attempts / minute PER credential + IP combination.
        // The previous `throttle:5,1` middleware keyed on IP only, which
        // meant (a) shared-NAT users throttled each other, and (b) an
        // attacker could split a 5/min budget across /login and /spa-login
        // to double their total attempts. This limiter is applied to BOTH
        // endpoints by name, so the bucket is shared. The email is lower-
        // cased and trimmed before hashing so "Alice@x" and "alice@x " map
        // to the same bucket.
        RateLimiter::for('auth-login', function (Request $request): Limit {
            $email = strtolower(trim((string) $request->input('email', '')));
            $bucket = $email !== '' ? 'e:'.sha1($email) : 'anon';
            $ip = $request->ip() ?? 'unknown';

            return Limit::perMinute(5)->by($bucket.'|'.$ip);
        });

        // queries: 30 queries / minute PER authenticated user. Shared
        // bucket across POST /queries (reserve) and POST /queries/{id}/start
        // (dispatch) so a single logical RAG query costs 1 slot, not 2.
        // Unauthenticated requests would never reach this route (it's behind
        // auth:sanctum) but fall back to IP just in case.
        RateLimiter::for('queries', function (Request $request): Limit {
            $key = $request->user()?->id
                ?? $request->ip()
                ?? 'anonymous-unknown';

            return Limit::perMinute(30)->by((string) $key);
        });

        // Phase H4 §7 — bridge:report-progress rate limit.
        // FastAPI POSTs to /api/internal/admin/reports/{build_id}/progress
        // from generate_report; even a runaway worker shouldn't be able to
        // saturate Reverb with broadcast traffic. 600 events/minute total
        // (~10/s) leaves ample headroom for the §15 12-node graph while
        // capping a stuck retry loop. Keyed on build_id from the URL so
        // one bad build doesn't drown out the others.
        RateLimiter::for('bridge:report-progress', function (Request $request): Limit {
            $buildId = (string) $request->route('build_id', 'unknown');

            return Limit::perMinute(600)->by('build:'.$buildId);
        });

        // ── Module 10 Chunk 10.4 — authz_audit → Prometheus counter ────
        //
        // Bridges the structured `authz.deny` events emitted by
        // {@see \App\Support\AuthorizationAuditLogger} into a cache-backed
        // counter that {@see \App\Http\Controllers\Internal\MetricsController}
        // exposes as `laravel_authz_deny_total{reason="..."}`. Until Module
        // 10.6 wires Loki, this is the authoritative export path.
        //
        // The cache counter survives Octane worker recycles because it lives
        // in Redis, not per-instance memory. The increment is best-effort —
        // a Redis blip drops the count for that event but never breaks the
        // request flow.
        Event::listen(MessageLogged::class, static function (MessageLogged $e): void {
            if (($e->context['event'] ?? null) !== 'authz.deny') {
                return;
            }
            $reason = (string) ($e->context['reason'] ?? 'unknown');
            try {
                Cache::increment("metrics:authz_deny:{$reason}");
            } catch (\Throwable) {
                // Cache backend unavailable; metric will simply lag. Do not
                // perturb the request that triggered the audit log.
            }
        });

        // ── project_user pivot boot guard (A1-01) ───────────────────
        //
        // The project_user pivot table is the single source of truth for
        // tenant isolation. If it is absent, User::hasProjectAccess() now
        // fails CLOSED (returns false), but a missing pivot is a misconfigured
        // environment — we refuse to serve web traffic rather than silently
        // deny every request.
        //
        // Octane lifecycle: this boot() method runs ONCE when the Octane
        // worker process starts, not per request. That is exactly the right
        // place for a startup health check. The guard is deliberately skipped
        // during `php artisan migrate` (and all other artisan commands) because
        // the table may not yet exist at that point — the migration that creates
        // it must be allowed to run. Unit tests are also excluded because they
        // run RefreshDatabase which drops and recreates tables between cases.
        if (! $this->app->runningInConsole()) {
            try {
                DB::table('project_user')->limit(1)->get();
            } catch (\Throwable $e) {
                throw new \RuntimeException(
                    'project_user pivot table is missing or unreadable — refusing to boot. '
                    .'Run `php artisan migrate` and ensure the database is reachable.',
                    0,
                    $e,
                );
            }
        }
    }
}
