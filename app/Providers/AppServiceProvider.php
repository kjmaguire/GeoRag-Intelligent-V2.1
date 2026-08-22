<?php

declare(strict_types=1);

namespace App\Providers;

use App\Models\User;
use App\Policies\DashboardPolicy;
use App\Services\Azure\AzureBlobDiskLifetime;
use App\Services\Azure\ManagedIdentityTokenProvider;
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
use Illuminate\Support\Facades\Log;
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
            // Managed-identity mode — no AccountKey ever touches this process.
            // The blob client authenticates with an Azure AD token fetched
            // from the Container App's system-assigned identity via IMDS
            // (see ManagedIdentityTokenProvider). Opt-in via
            // AZURE_STORAGE_AUTH_MODE=managed_identity; default stays
            // 'connection_string' (today's account-key behavior, unchanged)
            // so existing deployments are unaffected.
            //
            // SDK limitation: microsoft/azure-storage-blob ^1.1 has no
            // user-delegation-key SAS support (the AAD-token equivalent of
            // account-key SAS), so temporaryUrl() below still needs
            // AccountKey to sign presigned export/figure download URLs even
            // in managed-identity mode. That key is used ONLY for local SAS
            // signing — it never authenticates the actual blob read/write
            // traffic, which is 100% managed-identity in this mode. If
            // AZURE_STORAGE_CONNECTION_STRING isn't also set alongside
            // AZURE_STORAGE_AUTH_MODE=managed_identity, the buildTemporaryUrlsUsing
            // callback below never gets registered and temporaryUrl() throws
            // Flysystem's own UnableToGenerateTemporaryUrl — loud failure,
            // not a silently broken URL.
            if (($config['auth_mode'] ?? 'connection_string') === 'managed_identity') {
                // The token is COPIED into the client below and cannot be
                // changed afterwards — the SDK takes a bearer string, with
                // no setter and no callback. Laravel then caches this disk
                // on the FilesystemManager singleton, which under Octane
                // lives as long as the worker, so this closure runs once
                // and the token it captured is used until the worker dies.
                //
                // Record when that token expires so the RequestReceived
                // listener can drop the disk at that moment and force a
                // rebuild. Without it the worker serves 401s on every blob
                // operation from expiry until it happens to recycle —
                // OCTANE_MAX_REQUESTS=500 on an app this quiet means days.
                [$token, $expiresAt] = $app
                    ->make(ManagedIdentityTokenProvider::class)
                    ->getTokenWithExpiry();
                AzureBlobDiskLifetime::remember($expiresAt);
                $client = BlobRestProxy::createBlobServiceWithTokenCredential(
                    $token,
                    // DefaultEndpointsProtocol is required here even though it's
                    // always https — omitting it leaves the SDK's internal
                    // $scheme empty, producing a malformed "http://://..."
                    // endpoint URI (caught by AzureBlobDiskTest's managed-identity
                    // coverage).
                    'DefaultEndpointsProtocol=https;AccountName='.$config['account_name'],
                );
            } else {
                $client = BlobRestProxy::createBlobService($config['connection_string']);
            }
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
        // Octane lifecycle: this boot() method runs ONCE when the Octane
        // worker process starts, not per request. That is exactly the right
        // place for a startup health check. The guard is deliberately skipped
        // during `php artisan migrate` (and all other artisan commands) because
        // the table may not yet exist at that point — the migration that creates
        // it must be allowed to run. Unit tests are also excluded because they
        // run RefreshDatabase which drops and recreates tables between cases.
        //
        // `runningInConsole()` is false under Octane despite PHP_SAPI being
        // 'cli': vendor/laravel/octane/bin/bootstrap.php sets
        // $_ENV['APP_RUNNING_IN_CONSOLE'] = false before the app boots. So
        // this DOES run in the web tier, and only there — `artisan horizon`
        // and `artisan reverb:start` are console commands and skip it.
        if (! $this->app->runningInConsole()) {
            $this->guardProjectUserPivot();
        }
    }

    /**
     * Refuse to boot when the project_user pivot is missing, but NOT when the
     * database is merely unreachable.
     *
     * The pivot is the single source of truth for tenant isolation. If it is
     * absent, User::hasProjectAccess() fails CLOSED (returns false) — correct,
     * but a silent deny-everything is a worse failure than a loud one, so a
     * missing pivot still refuses web traffic.
     *
     * What this must NOT do is treat "cannot reach Postgres" as the same
     * condition. It used to: the guard caught \Throwable and turned every
     * failure into a fatal RuntimeException. On this deployment that is not
     * hypothetical — georag-pg-cc is deliberately Stopped 00:00–10:00 UTC by
     * the nightly cost schedule, so for ten hours a day any laravel-octane-cc
     * replica that restarts (a scale event, a node move, a CD rollout) threw
     * here, died, and crash-looped against a database that was down ON
     * PURPOSE. A deploy landing inside the window would fail its health check
     * for a reason entirely unrelated to the deploy.
     *
     * The two conditions want opposite responses:
     *
     *   - Pivot missing / unreadable → permanent, needs a human, and serving
     *     traffic would silently deny every request. Refuse to boot.
     *   - Database unreachable → transient, expected nightly, already
     *     monitored elsewhere. Boot; requests will surface their own errors,
     *     the health endpoint and static assets keep answering, and the very
     *     next request succeeds when Postgres returns — with no restart
     *     backoff and no failed revision.
     *
     * The discrimination is not new logic: User::isMissingProjectUserPivot()
     * has drawn exactly this line (SQLSTATE 42P01 / MySQL 1146) since A1-01.
     * The boot guard simply never used it. 42501 (insufficient_privilege) and
     * 3F000 (invalid_schema_name) are folded in here as equally permanent
     * misconfigurations — a GRANT gap presents as a readable-but-forbidden
     * table, which is just as fatal and just as human-fixable.
     */
    public function guardProjectUserPivot(): void
    {
        try {
            // Round-trip a trivial query first. This must be a real query,
            // not getPdo(): behind PgBouncer or Hyperdrive the client
            // connection is established before any server connection exists,
            // so a successful connect() says nothing about the backend.
            DB::selectOne('select 1');
        } catch (\Throwable $e) {
            Log::critical(
                'AppServiceProvider: database unreachable at boot — starting anyway. '
                .'The project_user pivot guard could not run, so tenancy has NOT been '
                .'verified for this worker. Requests needing the database will fail '
                .'until it returns.',
                ['exception' => $e->getMessage()],
            );

            return;
        }

        // The server answered, so anything that fails below is a property of
        // the schema or our grants on it — not of the network.
        try {
            DB::table('project_user')->limit(1)->get();
        } catch (\Throwable $e) {
            throw new \RuntimeException(
                'project_user pivot table is missing or unreadable — refusing to boot. '
                .'The database IS reachable, so this is a schema or privilege problem, '
                .'not an outage. Run `php artisan migrate` and check that the app role '
                .'has SELECT on project_user.',
                0,
                $e,
            );
        }
    }
}
