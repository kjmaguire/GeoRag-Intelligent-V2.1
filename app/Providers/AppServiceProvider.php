<?php

declare(strict_types=1);

namespace App\Providers;

use App\Models\User;
use App\Policies\DashboardPolicy;
use App\Support\Http\PooledHttpClient;
use Illuminate\Cache\RateLimiting\Limit;
use Illuminate\Http\Client\Factory as HttpFactory;
use Illuminate\Http\Request;
use Illuminate\Log\Events\MessageLogged;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Gate;
use Illuminate\Support\Facades\RateLimiter;
use Illuminate\Support\ServiceProvider;

class AppServiceProvider extends ServiceProvider
{
    /**
     * Register any application services.
     */
    public function register(): void
    {
        // PooledHttpClient — Guzzle client pool with TCP keep-alive per base
        // URL. Survives between requests in the same Octane worker so curl
        // sockets stay open to Martin / FastAPI / etc. State is bounded
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
        Gate::define('viewPortfolio', [DashboardPolicy::class, 'viewPortfolio']);
        Gate::define('viewProject', [DashboardPolicy::class, 'viewProject']);

        // Global admin gate — guards write access to shared resources such as
        // vendor profiles and column mappings. Reads directly from the users
        // table column; no role package is required at this scale.
        Gate::define('admin', fn (User $user): bool => (bool) $user->is_admin);

        // ── Rate limiters ────────────────────────────────────────────
        //
        // public-geoscience-tiles: 600 req/min per authenticated user.
        // MapLibre pan+zoom fires bursts of 40-80 tile GETs; 600/min
        // absorbs ~10 bursts/min sustained without letting a malicious
        // or misconfigured client drown Martin. Unauthenticated fallback
        // uses the client IP so a broken SPA can't starve everyone else
        // via a shared session cookie.
        RateLimiter::for('public-geoscience-tiles', function (Request $request): Limit {
            $key = $request->user()?->id
                ?? $request->ip()
                ?? 'anonymous-unknown';

            return Limit::perMinute(600)->by((string) $key);
        });

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

        // ── Per-workspace limiters (audit item F, 2026-06-03) ────────
        //
        // 'uploads' and 'charts' are keyed on workspace_id, not user_id.
        // Rationale: a malicious or runaway user inside workspace W
        // shouldn't be able to drain compute that other members of W
        // also share. Per-user keying would let one operator pin Dagster
        // queues for the whole team; per-workspace keying matches the
        // resource boundary the workspace itself enforces (RLS, queues,
        // billing). Anonymous fallback to IP so a broken SPA can't poison
        // every other anonymous request via a shared sentinel.
        //
        // Workspace resolution is fed by HandleInertiaRequests
        // (session('current_workspace_id') — landed with audit item A);
        // it falls back to the user's default workspace, then user id,
        // then IP, so the limiter still works for unauth+legacy requests.
        //
        // Artisan exemption is automatic: rate limiters only execute as
        // route middleware. `artisan ingest:reingest-project` and friends
        // never traverse this code path, so backfills aren't capped.
        $workspaceKey = static function (Request $request, string $prefix): string {
            $workspaceId = (string) ($request->session()->get('current_workspace_id') ?? '');
            if ($workspaceId === '') {
                $user = $request->user();
                if ($user !== null && method_exists($user, 'defaultWorkspaceId')) {
                    $workspaceId = (string) ($user->defaultWorkspaceId() ?? '');
                }
            }
            if ($workspaceId !== '') {
                return $prefix.':ws:'.$workspaceId;
            }
            // Fall back to user id, then IP, then sentinel.
            $userId = $request->user()?->id;
            if ($userId !== null) {
                return $prefix.':u:'.$userId;
            }

            return $prefix.':ip:'.($request->ip() ?? 'anonymous-unknown');
        };

        // 'uploads': 200 / hour per workspace.
        // Sized for normal exploration teams: typical bulk drop is
        // 50-150 files in one session; 200/hr leaves room for two such
        // bursts plus retries without bumping into the limit. A team
        // running an actual backfill should use artisan (which is exempt)
        // rather than the UI.
        RateLimiter::for('uploads', function (Request $request) use ($workspaceKey): Limit {
            return Limit::perHour(200)->by($workspaceKey($request, 'uploads'));
        });

        // 'charts': 60 / minute per workspace.
        // Chart renders are interactive: pan / filter / re-pivot can
        // fire 10-20 renders/min from a single tab. 60/min absorbs three
        // concurrent operators in one workspace without throttling, but
        // shuts down a runaway loop (looped frontend re-renders, bot
        // scraping the gallery) within a minute.
        RateLimiter::for('charts', function (Request $request) use ($workspaceKey): Limit {
            return Limit::perMinute(60)->by($workspaceKey($request, 'charts'));
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
