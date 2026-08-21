<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Http\Response;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Redis;
use Illuminate\Support\Facades\Schema;
use Laravel\Horizon\Contracts\MetricsRepository;
use Throwable;

/**
 * Module 10 Chunk 10.4 — hand-rolled Prometheus exposition endpoint.
 *
 * Closes audit finding H-A1-01 (Prometheus expects /metrics, Laravel never exposed
 * it — all Pulse-derived signals were silently 404).
 *
 * Why hand-rolled
 * ---------------
 * The metric set is small (~10 series) and relies on data Pulse + Horizon + Octane
 * already cache. Pulling in `promphp/prometheus_client_php` would add a transitive
 * dep tree for marginal benefit. The exposition format is plain text with three
 * lines per series (HELP, TYPE, value); writing it directly is ~50 lines.
 *
 * Authentication posture (per audit)
 * ----------------------------------
 * `/metrics` is unauthenticated by design — Prometheus needs to scrape without
 * carrying a session. The endpoint is firewalled at the Docker network layer
 * (port 80 inside the compose network is NOT exposed externally) and at the
 * application layer via {@see self::isAllowedScraper()} which only admits
 * private-IP callers. Public deployments must add nginx-level allow/deny rules
 * (documented in `ops/runbooks/secret-management.md`).
 *
 * Octane-safe
 * -----------
 * No per-instance state. Each request reads fresh from Cache + DB + Redis.
 * `metricsTextLines()` returns a list each call, no buffering between requests.
 */
final class MetricsController extends Controller
{
    /**
     * GET /metrics  — Prometheus exposition.
     */
    public function __invoke(Request $request): Response
    {
        if (! $this->isAllowedScraper($request)) {
            return new Response('forbidden', 403, ['Content-Type' => 'text/plain']);
        }

        $lines = [];
        try {
            $lines = array_merge($lines, $this->horizonQueueDepth());
        } catch (Throwable $e) {
            $lines[] = '# warning: horizon_queue_depth unavailable: '.$e->getMessage();
        }

        try {
            $lines = array_merge($lines, $this->octaneWorkers());
        } catch (Throwable $e) {
            $lines[] = '# warning: octane_workers unavailable: '.$e->getMessage();
        }

        try {
            $lines = array_merge($lines, $this->pulseExceptions());
        } catch (Throwable $e) {
            $lines[] = '# warning: pulse_exception_total unavailable: '.$e->getMessage();
        }

        try {
            $lines = array_merge($lines, $this->pulseSlowQueries());
        } catch (Throwable $e) {
            $lines[] = '# warning: slow_queries_total unavailable: '.$e->getMessage();
        }

        try {
            $lines = array_merge($lines, $this->pulseCacheHitRatio());
        } catch (Throwable $e) {
            $lines[] = '# warning: cache_hit_ratio unavailable: '.$e->getMessage();
        }

        try {
            $lines = array_merge($lines, $this->authzAuditCounter());
        } catch (Throwable $e) {
            $lines[] = '# warning: laravel_authz_deny_total unavailable: '.$e->getMessage();
        }

        // `dagster_runs_total` was emitted here until the Dagster tree was
        // retired. It had been dead on Azure the whole time: the query
        // hard-coded host=postgresql:5432, a compose-only hostname, so every
        // scrape tick logged dagster_metrics_query_failed and appended a
        // `# warning:` line to the scrape body.

        // V1.5-08 — Reverb broadcast volume via Pulse aggregates (cache_set
        // events on the broadcast channel). Reverb itself doesn't expose a
        // scrapable HTTP endpoint without Pusher HMAC auth, so we surface the
        // Laravel-side counter instead.
        try {
            $lines = array_merge($lines, $this->reverbBroadcastCounter());
        } catch (Throwable $e) {
            $lines[] = '# warning: reverb_broadcasts_total unavailable: '.$e->getMessage();
        }

        $lines[] = '# EOF';

        return new Response(
            implode("\n", $lines)."\n",
            200,
            ['Content-Type' => 'text/plain; version=0.0.4; charset=utf-8'],
        );
    }

    /**
     * Only admit private-network callers. The Prometheus server in the same
     * compose network sees an internal IP. Public traffic is rejected.
     *
     * Module 9 Chunk 9.5 wired TrustProxies, so `$request->ip()` already
     * reflects the X-Forwarded-For chain. We check the resulting client IP
     * against RFC 1918 + loopback ranges.
     */
    private function isAllowedScraper(Request $request): bool
    {
        $ip = (string) $request->ip();
        if ($ip === '' || $ip === '::1' || $ip === '127.0.0.1') {
            return true;
        }
        // Match IPv4 RFC 1918 + loopback. IPv6 ULA (fc00::/7) accepted via str-prefix.
        if (preg_match('/^10\./', $ip)) {
            return true;
        }
        if (preg_match('/^192\.168\./', $ip)) {
            return true;
        }
        if (preg_match('/^172\.(1[6-9]|2[0-9]|3[0-1])\./', $ip)) {
            return true;
        }
        if (str_starts_with($ip, 'fc') || str_starts_with($ip, 'fd')) {
            return true;
        }

        return false;
    }

    /** @return list<string> */
    private function horizonQueueDepth(): array
    {
        if (! class_exists(MetricsRepository::class)) {
            return ['# horizon_queue_depth: Horizon not installed'];
        }

        $lines = [
            '# HELP horizon_queue_depth Pending jobs per Horizon queue',
            '# TYPE horizon_queue_depth gauge',
        ];

        $queues = (array) config('horizon.defaults.queue', ['default']);
        if (empty($queues)) {
            $queues = ['default'];
        }

        foreach ((array) $queues as $queue) {
            try {
                $depth = (int) Redis::connection('horizon')->llen("queues:{$queue}");
            } catch (Throwable) {
                $depth = 0;
            }
            $lines[] = sprintf('horizon_queue_depth{queue="%s"} %d', $queue, $depth);
        }

        return $lines;
    }

    /** @return list<string> */
    private function octaneWorkers(): array
    {
        // Octane exposes worker stats via its own server; we approximate
        // busy-ratio from the request-in-flight cache key the runtime maintains.
        $busy = (int) Cache::get('octane:workers:busy', 0);
        $total = (int) Cache::get('octane:workers:total', max(1, (int) config('services.octane_metrics.workers')));

        return [
            '# HELP octane_workers_busy Currently-busy Octane workers',
            '# TYPE octane_workers_busy gauge',
            sprintf('octane_workers_busy %d', $busy),
            '# HELP octane_workers_total Total Octane workers',
            '# TYPE octane_workers_total gauge',
            sprintf('octane_workers_total %d', $total),
        ];
    }

    /** @return list<string> */
    private function pulseExceptions(): array
    {
        // Pulse stores exception aggregates in `pulse_aggregates` (type='exception').
        // Roll up the last 5 minutes by class.
        $lines = [
            '# HELP pulse_exception_total Exceptions captured by Pulse in the last 5 minutes',
            '# TYPE pulse_exception_total counter',
        ];
        $rows = $this->pulseAggregateRollup('exception', 300);
        foreach ($rows as $row) {
            $lines[] = sprintf(
                'pulse_exception_total{class="%s"} %d',
                $this->escapeLabelValue((string) ($row->key ?? 'unknown')),
                (int) ($row->total ?? 0),
            );
        }
        if (count($rows) === 0) {
            $lines[] = 'pulse_exception_total{class="none"} 0';
        }

        return $lines;
    }

    /** @return list<string> */
    private function pulseSlowQueries(): array
    {
        $lines = [
            '# HELP slow_queries_total Slow queries captured by Pulse in the last 5 minutes',
            '# TYPE slow_queries_total counter',
        ];
        $rows = $this->pulseAggregateRollup('slow_query', 300);
        foreach ($rows as $row) {
            $lines[] = sprintf(
                'slow_queries_total{connection="%s"} %d',
                $this->escapeLabelValue((string) ($row->key ?? 'unknown')),
                (int) ($row->total ?? 0),
            );
        }
        if (count($rows) === 0) {
            $lines[] = 'slow_queries_total{connection="none"} 0';
        }

        return $lines;
    }

    /** @return list<string> */
    private function pulseCacheHitRatio(): array
    {
        $lines = [
            '# HELP cache_hit_ratio Cache hit ratio per store, last 5 minutes',
            '# TYPE cache_hit_ratio gauge',
        ];

        // Pulse cache_interaction aggregates store hit + miss counts separately.
        $hits = $this->pulseAggregateRollup('cache_hit', 300);
        $miss = $this->pulseAggregateRollup('cache_miss', 300);
        $missByKey = [];
        foreach ($miss as $row) {
            $missByKey[(string) ($row->key ?? 'unknown')] = (int) ($row->total ?? 0);
        }
        $emitted = false;
        foreach ($hits as $row) {
            $store = (string) ($row->key ?? 'unknown');
            $h = (int) ($row->total ?? 0);
            $m = $missByKey[$store] ?? 0;
            $denom = $h + $m;
            $ratio = $denom > 0 ? $h / $denom : 0.0;
            $lines[] = sprintf(
                'cache_hit_ratio{store="%s"} %.4f',
                $this->escapeLabelValue($store),
                $ratio,
            );
            $emitted = true;
        }
        if (! $emitted) {
            $lines[] = 'cache_hit_ratio{store="none"} 0';
        }

        return $lines;
    }

    /** @return list<string> */
    private function authzAuditCounter(): array
    {
        // Module 9 Chunk 9.8 — read from authz_audit log channel via a tiny
        // counter stored in cache. The MessageLogged listener (registered in
        // a service provider) increments this on every authz.deny event.
        // Until 10.6's Loki integration provides log-derived metrics, this
        // cache-backed counter is the authoritative export.
        $lines = [
            '# HELP laravel_authz_deny_total Cumulative authz.deny events by reason',
            '# TYPE laravel_authz_deny_total counter',
        ];

        $reasons = ['no_pivot_row', 'cross_workspace', 'unauthenticated', 'cross_user', 'admin_only'];
        $emitted = false;
        foreach ($reasons as $reason) {
            $count = (int) Cache::get("metrics:authz_deny:{$reason}", 0);
            if ($count > 0 || $reason === 'no_pivot_row') {
                // Always emit no_pivot_row even when zero so dashboards have
                // a stable series.
                $lines[] = sprintf('laravel_authz_deny_total{reason="%s"} %d', $reason, $count);
                $emitted = true;
            }
        }
        if (! $emitted) {
            $lines[] = 'laravel_authz_deny_total{reason="none"} 0';
        }

        return $lines;
    }

    /**
     * V1.5-08 — Reverb broadcast counter from Pulse aggregates.
     *
     * Pulse's `cache_set` events with key prefix `reverb_broadcast_*` track
     * every WebSocket message Laravel pushes through Reverb. Surface as a
     * counter-style gauge so the dashboard can show broadcast volume even
     * though Reverb itself isn't scrapable without Pusher HMAC auth.
     *
     * @return list<string>
     */
    private function reverbBroadcastCounter(): array
    {
        $lines = [
            '# HELP reverb_broadcasts_total Reverb WebSocket broadcasts in the last 5 minutes (Pulse-derived)',
            '# TYPE reverb_broadcasts_total counter',
        ];

        // Pulse type for Laravel broadcasts is conventionally `cache_hit` /
        // `cache_set` with `key` like `reverb:*`. Filter narrowly to avoid
        // double-counting unrelated cache traffic.
        $rows = $this->pulseAggregateRollup('cache_set', 300);
        $total = 0;
        foreach ($rows as $row) {
            $key = (string) ($row->key ?? '');
            if (! str_starts_with($key, 'reverb')) {
                continue;
            }
            $total += (int) ($row->total ?? 0);
        }

        $lines[] = sprintf('reverb_broadcasts_total %d', $total);

        return $lines;
    }

    /**
     * Read a recent rollup from Pulse's aggregate table.
     *
     * @return array<int,object>
     */
    private function pulseAggregateRollup(string $type, int $windowSeconds): array
    {
        if (! Schema::hasTable('pulse_aggregates')) {
            return [];
        }

        $since = now()->subSeconds($windowSeconds);

        return DB::connection(config('pulse.storage.database.connection', config('database.default')))
            ->table('pulse_aggregates')
            ->where('type', $type)
            ->where('bucket', '>=', $since->timestamp)
            ->groupBy('key')
            ->selectRaw('key, SUM(value) as total')
            ->limit(50)
            ->get()
            ->toArray();
    }

    /**
     * Prometheus label values escape: backslash, quote, newline.
     */
    private function escapeLabelValue(string $value): string
    {
        return strtr($value, [
            '\\' => '\\\\',
            '"' => '\\"',
            "\n" => '\\n',
        ]);
    }
}
