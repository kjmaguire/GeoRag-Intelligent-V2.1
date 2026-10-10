<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Http\Controllers\Controller;
use App\Support\AuthorizationAuditLogger;
use Illuminate\Http\Response;
use Illuminate\Queue\RedisQueue;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Facades\Schema;
use Laravel\Horizon\Contracts\MetricsRepository;
use RuntimeException;
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
 * Authentication posture
 * ----------------------
 * `/metrics` requires the `service.key` shared secret (`X-Service-Key`), the
 * same one the internal FastAPI callbacks use. A scraper carries a header, not
 * a session, so this costs it nothing.
 *
 * It used to be gated on `$request->ip()` being an RFC-1918 address instead.
 * That is not a control the application can verify: `ip()` reads the
 * X-Forwarded-For chain, the chain is client-supplied, and production trusts
 * every proxy — so `X-Forwarded-For: 10.0.0.1` was enough for any anonymous
 * caller on the internet to read Horizon queue depths, Pulse exception counts,
 * slow-query counts and authz-deny counters. Confirmed live against the
 * production ingress during the 2026-08-20 review.
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
    public function __invoke(): Response
    {
        $lines = [];
        try {
            $lines = array_merge($lines, $this->horizonQueueDepth());
        } catch (Throwable $e) {
            $lines[] = '# warning: horizon_queue_depth unavailable: '.$this->oneLine($e->getMessage());
        }

        try {
            $lines = array_merge($lines, $this->octaneWorkers());
        } catch (Throwable $e) {
            $lines[] = '# warning: octane_workers unavailable: '.$this->oneLine($e->getMessage());
        }

        try {
            $lines = array_merge($lines, $this->pulseExceptions());
        } catch (Throwable $e) {
            $lines[] = '# warning: pulse_exception_total unavailable: '.$this->oneLine($e->getMessage());
        }

        try {
            $lines = array_merge($lines, $this->pulseSlowQueries());
        } catch (Throwable $e) {
            $lines[] = '# warning: slow_queries_total unavailable: '.$this->oneLine($e->getMessage());
        }

        try {
            $lines = array_merge($lines, $this->pulseCacheHitRatio());
        } catch (Throwable $e) {
            $lines[] = '# warning: cache_hit_ratio unavailable: '.$this->oneLine($e->getMessage());
        }

        try {
            $lines = array_merge($lines, $this->authzAuditCounter());
        } catch (Throwable $e) {
            $lines[] = '# warning: laravel_authz_deny_total unavailable: '.$this->oneLine($e->getMessage());
        }

        // V1.5-08 — Reverb broadcast volume via Pulse aggregates (cache_set
        // events on the broadcast channel). Reverb itself doesn't expose a
        // scrapable HTTP endpoint without Pusher HMAC auth, so we surface the
        // Laravel-side counter instead.
        try {
            $lines = array_merge($lines, $this->reverbBroadcastCounter());
        } catch (Throwable $e) {
            $lines[] = '# warning: reverb_broadcasts_total unavailable: '.$this->oneLine($e->getMessage());
        }

        $lines[] = '# EOF';

        return new Response(
            implode("\n", $lines)."\n",
            200,
            ['Content-Type' => 'text/plain; version=0.0.4; charset=utf-8'],
        );
    }

    /** @return list<string> */
    private function horizonQueueDepth(): array
    {
        // interface_exists, not class_exists: the Horizon MetricsRepository
        // is an INTERFACE, and class_exists() returns false for interfaces
        // — so this guard was always true, and
        // horizon_queue_depth has never emitted a single sample despite
        // Horizon being a hard composer requirement with a running container
        // app. The one metric that would show the `llm` queue backing up
        // reported "not installed" instead, so no alert could ever be built
        // on it.
        if (! interface_exists(MetricsRepository::class) && ! class_exists(MetricsRepository::class)) {
            return ['# horizon_queue_depth: Horizon not installed'];
        }

        $lines = [
            '# HELP horizon_queue_depth Pending jobs per Horizon queue',
            '# TYPE horizon_queue_depth gauge',
        ];

        foreach ($this->horizonQueues() as ['connection' => $connection, 'queue' => $queue]) {
            try {
                $depth = $this->pendingJobs($connection, $queue);
            } catch (Throwable $e) {
                // NO sample for a queue that could not be read. This used to
                // catch and report 0, which is indistinguishable from an empty
                // queue -- and 0 is what it reported for every queue, always
                // (see pendingJobs()). A missing series is what an absent()
                // alert exists to catch; a zero is what it can never catch.
                $lines[] = '# warning: '.sprintf('horizon_queue_depth{queue="%s"}', $queue)
                    .' unavailable: '.$this->oneLine($e->getMessage());

                continue;
            }
            $lines[] = sprintf('horizon_queue_depth{queue="%s"} %d', $queue, $depth);
        }

        return $lines;
    }

    /**
     * Jobs waiting on one queue, read through the queue's OWN connection.
     *
     * The old read was `Redis::connection('horizon')->llen("queues:{$queue}")`,
     * which could never see a job. Horizon's `horizon` Redis connection is a
     * copy of the default one with its key prefix REPLACED by horizon.prefix
     * (Laravel\Horizon\Horizon::configureStandaloneConnection) because it holds
     * Horizon's own bookkeeping; the jobs live under the prefix of the Redis
     * connection the queue connection names (`queue.connections.*.connection`,
     * REDIS_QUEUE_CONNECTION), with that connection's own prefix. So the key it
     * counted, `<horizon prefix>queues:default`, does not exist, llen of a
     * missing key is 0, and the gauge sat at 0 while the `llm` queue backed up.
     *
     * The queue connection knows its Redis connection, prefix and (on Redis
     * Cluster) hash-tagged key, so ask it: Queue::connection()->pendingSize()
     * is llen on exactly the list workers pop from.
     */
    private function pendingJobs(string $connection, string $queue): int
    {
        $driver = Queue::connection($connection);

        if (! $driver instanceof RedisQueue) {
            throw new RuntimeException("queue connection [{$connection}] is not a Redis queue");
        }

        return (int) $driver->pendingSize($queue);
    }

    /**
     * Every queue any Horizon supervisor is configured to consume, with the
     * queue connection it consumes it on.
     *
     * `horizon.defaults` is keyed by SUPERVISOR NAME, so `defaults.queue`
     * is not a path that exists — the real ones are
     * `defaults.supervisor-1.queue` and `defaults.supervisor-llm.queue`.
     * Reading the non-existent key returned null and fell through to the
     * `['default']` fallback, so the fixed guard above restored the metric
     * for exactly one queue and left out `llm` — the queue the comment
     * above says the metric exists to watch, and the one that actually
     * backs up, because a stuck LLM stream holds its worker for the full
     * 300-second job timeout.
     *
     * The connection matters as much as the name: `llm` is consumed on
     * `redis-llm`, not `redis`. Derived rather than listed so adding a
     * supervisor to config/horizon.php is enough; there is no second copy
     * here to forget.
     *
     * @return list<array{connection: string, queue: string}>
     */
    private function horizonQueues(): array
    {
        $defaults = (array) config('horizon.defaults', []);
        $found = [];

        $collect = static function (string $supervisorName, array $supervisor) use (&$found, $defaults): void {
            // An environment block names only what it overrides; the rest is
            // the same supervisor's entry in `defaults`.
            $connection = $supervisor['connection'] ?? $defaults[$supervisorName]['connection'] ?? 'redis';
            $queues = $supervisor['queue'] ?? $defaults[$supervisorName]['queue'] ?? [];

            foreach ((array) $queues as $queue) {
                if (is_string($connection) && is_string($queue) && $queue !== '') {
                    $found["{$connection}\0{$queue}"] = ['connection' => $connection, 'queue' => $queue];
                }
            }
        };

        foreach ($defaults as $supervisorName => $supervisor) {
            $collect((string) $supervisorName, (array) $supervisor);
        }

        // Environment blocks may add supervisors the defaults don't declare.
        foreach ((array) config('horizon.environments', []) as $supervisors) {
            foreach ((array) $supervisors as $supervisorName => $supervisor) {
                $collect((string) $supervisorName, (array) $supervisor);
            }
        }

        return $found === []
            ? [['connection' => 'redis', 'queue' => 'default']]
            : array_values($found);
    }

    /**
     * A message safe to put on one `# warning:` line of the exposition: an
     * exception message with a newline in it would otherwise end the comment
     * and let the rest be read as a sample.
     */
    private function oneLine(string $message): string
    {
        return trim((string) preg_replace('/\s+/', ' ', $message));
    }

    /** @return list<string> */
    private function octaneWorkers(): array
    {
        // `octane_workers_total` is real: services.octane_metrics.workers
        // reads the same OCTANE_WORKERS the container start command passes
        // to `octane:start --workers`.
        $total = (int) Cache::get(
            'octane:workers:total',
            max(1, (int) config('services.octane_metrics.workers')),
        );

        $lines = [
            '# HELP octane_workers_total Total Octane workers',
            '# TYPE octane_workers_total gauge',
            sprintf('octane_workers_total %d', $total),
        ];

        // `octane_workers_busy` used to be emitted unconditionally from
        // `Cache::get('octane:workers:busy', 0)` — a key NOTHING in this
        // repository writes. A busy gauge that can never rise is not an
        // unmeasured signal, it is a WRONG one: it renders as a flat,
        // healthy-looking zero on the Service Health dashboard while the
        // 4-worker, maxReplicas=1 deployment is saturated. Emitting a
        // fabricated constant is worse than emitting nothing.
        //
        // Emitted only if something actually wrote it, so a future writer
        // lights this up with no change here. There is no in-request route
        // to Swoole's `$server->stats()` — Octane binds no server instance
        // into the container — so a real busy count needs a tick listener
        // (see config/octane.php's `tick` hooks) writing worker stats into
        // the Octane cache table. Until that exists, absent is honest.
        if (Cache::has('octane:workers:busy')) {
            $lines[] = '# HELP octane_workers_busy Currently-busy Octane workers';
            $lines[] = '# TYPE octane_workers_busy gauge';
            $lines[] = sprintf(
                'octane_workers_busy %d',
                (int) Cache::get('octane:workers:busy'),
            );
        }

        return $lines;
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

        // The reasons the application actually writes
        // (AuthorizationAuditLogger::REASONS, held to the call sites by a
        // test), each emitted even at zero so a dashboard has a stable series
        // -- and `increase()` sees the first deny rather than a series that
        // starts at 1.
        //
        // This used to be a hand-kept list of five, three of which nothing
        // emits (cross_workspace, unauthenticated, cross_user, admin_only
        // were the retired ChatConversation / column-mapping gates), and it
        // omitted `not_project_owner`, which ProjectController writes on
        // every owner-only denial: those were counted in the cache and never
        // exported.
        foreach (AuthorizationAuditLogger::REASONS as $reason) {
            $lines[] = sprintf(
                'laravel_authz_deny_total{reason="%s"} %d',
                $reason,
                (int) Cache::get("metrics:authz_deny:{$reason}", 0),
            );
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
