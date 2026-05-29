<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1\PublicGeoscience;

use App\Http\Controllers\Controller;
use App\Support\Http\PooledHttpClient;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Redis;
use Throwable;

/**
 * Aggregated health check for the Public Geoscience surface.
 *
 * GET /api/v1/public-geoscience/health
 *
 * Returns a single JSON payload that ops dashboards, Prometheus blackbox
 * exporters, or uptime monitors can poll. Checks:
 *
 *   1. PostGIS — canonical table row counts for active jurisdictions.
 *   2. Staleness — max age across all active source last_refreshed_at.
 *   3. Martin — reachability of the tile server's /health endpoint.
 *   4. Qdrant — point counts per Public Geoscience collection.
 *
 * Cached for 60 seconds to avoid hammering downstream services on
 * high-frequency polls. The check is lightweight (4 SQL counts + 2
 * HTTP GETs) so the cache is more about being a good neighbour than
 * about performance.
 *
 * Auth: Sanctum-protected like all other PG endpoints. An ops service
 * account can use a long-lived Personal Access Token for polling.
 */
class HealthController extends Controller
{
    private const CACHE_KEY = 'public-geoscience:health:v1';
    private const CACHE_TTL = 60;

    // Staleness thresholds (seconds). Configurable if needed via config/services.php.
    private const STALENESS_WARN_SECONDS = 86_400 * 3;   // 3 days
    private const STALENESS_CRIT_SECONDS = 86_400 * 10;  // 10 days

    public function __invoke(): JsonResponse
    {
        $payload = Cache::remember(self::CACHE_KEY, self::CACHE_TTL, fn () => $this->check());

        // HTTP status: 200 if all green/warn, 503 if any critical.
        $status = ($payload['overall'] === 'critical') ? 503 : 200;

        return response()->json($payload, $status);
    }

    private function check(): array
    {
        $checks = [];
        $overall = 'green';

        // ── 1. PostGIS canonical row counts ──────────────────────────
        try {
            $rows = DB::select("
                SELECT 'mine' AS t, COUNT(*) AS n FROM public_geo.pg_mine
                UNION ALL SELECT 'mineral_occurrence', COUNT(*) FROM public_geo.pg_mineral_occurrence
                UNION ALL SELECT 'drillhole_collar', COUNT(*) FROM public_geo.pg_drillhole_collar
                UNION ALL SELECT 'resource_potential_zone', COUNT(*) FROM public_geo.pg_resource_potential_zone
                UNION ALL SELECT 'rock_sample', COUNT(*) FROM public_geo.pg_rock_sample
                UNION ALL SELECT 'assessment_survey', COUNT(*) FROM public_geo.pg_assessment_survey
                UNION ALL SELECT 'mineral_disposition', COUNT(*) FROM public_geo.pg_mineral_disposition
            ");
            $counts = [];
            $total = 0;
            foreach ($rows as $r) {
                $counts[$r->t] = (int) $r->n;
                $total += (int) $r->n;
            }
            $tableCount = count($counts);
            $checks['postgis'] = [
                'status' => $total > 0 ? 'green' : 'warn',
                'message' => $total > 0
                    ? "{$total} canonical rows across {$tableCount} tables"
                    : 'No canonical data — have Bronze + Silver pipelines run?',
                'counts' => $counts,
            ];
            if ($total === 0) {
                $overall = 'warn';
            }
        } catch (Throwable $e) {
            $checks['postgis'] = ['status' => 'critical', 'message' => $e->getMessage()];
            $overall = 'critical';
        }

        // ── 2. Staleness — worst-case across active sources ─────────
        try {
            $stalest = DB::selectOne("
                SELECT
                    s.source_id,
                    s.last_refreshed_at,
                    EXTRACT(EPOCH FROM (NOW() - s.last_refreshed_at))::BIGINT AS staleness_seconds
                  FROM public_geo.sources s
                  JOIN public_geo.jurisdictions j
                       ON j.jurisdiction_code = s.jurisdiction_code
                 WHERE j.status = 'active'
                   AND s.last_refreshed_at IS NOT NULL
                 ORDER BY s.last_refreshed_at ASC
                 LIMIT 1
            ");

            if ($stalest === null) {
                $checks['staleness'] = [
                    'status' => 'warn',
                    'message' => 'No sources have been refreshed yet',
                    'stalest_source' => null,
                    'staleness_seconds' => null,
                ];
                if ($overall !== 'critical') {
                    $overall = 'warn';
                }
            } else {
                $age = (int) $stalest->staleness_seconds;
                $level = $age > self::STALENESS_CRIT_SECONDS ? 'critical'
                       : ($age > self::STALENESS_WARN_SECONDS ? 'warn' : 'green');
                $checks['staleness'] = [
                    'status' => $level,
                    'message' => "Stalest source: {$stalest->source_id} ({$this->humanAge($age)})",
                    'stalest_source' => $stalest->source_id,
                    'staleness_seconds' => $age,
                    'last_refreshed_at' => $stalest->last_refreshed_at,
                ];
                if ($level === 'critical') {
                    $overall = 'critical';
                } elseif ($level === 'warn' && $overall !== 'critical') {
                    $overall = 'warn';
                }
            }
        } catch (Throwable $e) {
            $checks['staleness'] = ['status' => 'critical', 'message' => $e->getMessage()];
            $overall = 'critical';
        }

        // ── 3. Martin tile server reachability ──────────────────────
        try {
            $martinUrl = rtrim((string) config('services.martin.internal_url'), '/');
            $resp = app(PooledHttpClient::class)
                ->forBaseUrl($martinUrl, 5)
                ->get("{$martinUrl}/health");
            $checks['martin'] = [
                'status' => $resp->successful() ? 'green' : 'warn',
                'message' => $resp->successful()
                    ? 'Martin tile server reachable'
                    : "Martin returned HTTP {$resp->status()}",
                'url' => "{$martinUrl}/health",
            ];
            if (! $resp->successful() && $overall !== 'critical') {
                $overall = 'warn';
            }
        } catch (Throwable $e) {
            $checks['martin'] = [
                'status' => 'critical',
                'message' => "Martin unreachable: {$e->getMessage()}",
            ];
            $overall = 'critical';
        }

        // ── 4. Qdrant collections ───────────────────────────────────
        try {
            $qdrantHost = (string) config('services.qdrant.host');
            $qdrantPort = (int) config('services.qdrant.port');
            $qdrantUrl = "http://{$qdrantHost}:{$qdrantPort}";
            $resp = app(PooledHttpClient::class)
                ->forBaseUrl($qdrantUrl, 5)
                ->get("{$qdrantUrl}/collections");
            if ($resp->successful()) {
                $collections = collect($resp->json('result.collections', []))
                    ->filter(fn ($c) => str_starts_with($c['name'] ?? '', 'pg_'))
                    ->pluck('name')
                    ->values()
                    ->all();
                $checks['qdrant'] = [
                    'status' => count($collections) >= 4 ? 'green' : 'warn',
                    'message' => count($collections) . ' PG collections found',
                    'collections' => $collections,
                ];
            } else {
                $checks['qdrant'] = ['status' => 'warn', 'message' => "Qdrant HTTP {$resp->status()}"];
                if ($overall !== 'critical') {
                    $overall = 'warn';
                }
            }
        } catch (Throwable $e) {
            $checks['qdrant'] = ['status' => 'critical', 'message' => "Qdrant unreachable: {$e->getMessage()}"];
            $overall = 'critical';
        }

        // ── 5. Redis (cache + queue + sessions backbone) ────────────
        // PING the default Redis connection. In dev this is the single
        // instance; in staging/prod (3-instance topology) it's the cache
        // instance. A failure here means: app cache misses (degraded),
        // queue dispatch failing (jobs queue locally and stall), session
        // store unavailable (forced re-login storm). Redis being unreachable
        // is a critical condition for the app even though tile reads still
        // work via Martin → PG.
        try {
            $start = microtime(true);
            $pong = Redis::connection()->ping();
            $latencyMs = (int) round((microtime(true) - $start) * 1000);
            $checks['redis'] = [
                'status'     => $pong ? 'green' : 'warn',
                'message'    => $pong
                    ? "PONG in {$latencyMs}ms"
                    : 'PING returned falsy — Redis responded but not as expected',
                'latency_ms' => $latencyMs,
            ];
            if (! $pong && $overall !== 'critical') {
                $overall = 'warn';
            }
        } catch (Throwable $e) {
            $checks['redis'] = ['status' => 'critical', 'message' => "Redis unreachable: {$e->getMessage()}"];
            $overall = 'critical';
        }

        return [
            'overall' => $overall,
            'checked_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => self::CACHE_TTL,
            'checks' => $checks,
        ];
    }

    private function humanAge(int $seconds): string
    {
        if ($seconds < 60) return 'just now';
        if ($seconds < 3600) return round($seconds / 60) . ' min ago';
        if ($seconds < 86_400) return round($seconds / 3600) . ' hours ago';
        return round($seconds / 86_400) . ' days ago';
    }
}
