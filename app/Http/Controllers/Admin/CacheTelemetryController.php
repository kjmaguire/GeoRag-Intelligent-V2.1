<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 37 R-P21-CACHE-TELEMETRY-DASHBOARD Step 1 — JSON API for the
 * cache health view. Aggregates `silver.answer_runs` cache columns
 * over rolling windows so the frontend (R-P11-B) can render
 * dashboard charts without re-implementing the SQL on every page.
 *
 * Auth: 'admin' Gate (users.is_admin = true), same as the other
 * Admin/* controllers.
 *
 * Route: GET /admin/cache-telemetry/skip-reasons.json (web.php).
 *
 * Returned shape:
 *   {
 *     "window_hours": 24,
 *     "totals": {
 *       "hits": int,            -- runs with cache_hit_of_run_id IS NOT NULL
 *       "misses": int,          -- runs with cache_hit_of_run_id IS NULL
 *       "total": int,
 *       "hit_rate": float       -- hits / total, or 0.0 when total=0
 *     },
 *     "skipped_reasons": {
 *       "zero_candidates": int,
 *       "partial_failures": int,
 *       "schema_validation_failed": int,
 *       "downhole_bypass_legacy": int,
 *       "(none)": int           -- cache_skipped_reason IS NULL
 *     },
 *     "last_hour": {<same shape as totals>}
 *   }
 *
 * Read-only. Idempotent. Cheap — bounded to recent rows via the
 * created_at index on silver.answer_runs.
 */
class CacheTelemetryController extends Controller
{
    /**
     * Phase 38 R-P21 frontend slice — Inertia page that consumes the
     * JSON endpoint below. The page is self-fetching (it calls
     * /admin/cache-telemetry/skip-reasons.json on mount); this method
     * just authorizes admin access and serves the page shell.
     */
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        return Inertia::render('Admin/CacheTelemetry');
    }

    public function skipReasons(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $windowHours = (int) $request->query('window_hours', '24');
        if ($windowHours < 1 || $windowHours > 168) {
            $windowHours = 24;
        }

        $totals24h = $this->cacheTotals($windowHours);
        $totals1h = $this->cacheTotals(1);
        $skipped = $this->skippedReasonBreakdown($windowHours);

        return response()->json([
            'window_hours' => $windowHours,
            'totals' => $totals24h,
            'skipped_reasons' => $skipped,
            'last_hour' => $totals1h,
        ]);
    }

    /**
     * Hits / misses aggregation over the last $hours of answer_runs.
     *
     * @return array{hits:int,misses:int,total:int,hit_rate:float}
     */
    private function cacheTotals(int $hours): array
    {
        $row = DB::connection('pgsql')
            ->table('silver.answer_runs')
            ->selectRaw(
                'count(*) FILTER (WHERE cache_hit_of_run_id IS NOT NULL) AS hits, '
                .'count(*) FILTER (WHERE cache_hit_of_run_id IS NULL) AS misses, '
                .'count(*) AS total'
            )
            ->where('created_at', '>=', DB::raw("now() - interval '{$hours} hour'"))
            ->first();

        $hits = (int) ($row->hits ?? 0);
        $misses = (int) ($row->misses ?? 0);
        $total = (int) ($row->total ?? 0);
        $hitRate = $total > 0 ? round($hits / $total, 4) : 0.0;

        return [
            'hits' => $hits,
            'misses' => $misses,
            'total' => $total,
            'hit_rate' => $hitRate,
        ];
    }

    /**
     * Per-reason counts of cache writes that were skipped (Phase 21
     * + Phase 30 R-P21-CACHE-SKIPPED-REASON). NULL is folded into
     * "(none)" so the consumer doesn't have to special-case it.
     *
     * @return array<string,int>
     */
    private function skippedReasonBreakdown(int $hours): array
    {
        $rows = DB::connection('pgsql')
            ->table('silver.answer_runs')
            ->select('cache_skipped_reason', DB::raw('count(*) AS n'))
            ->where('created_at', '>=', DB::raw("now() - interval '{$hours} hour'"))
            ->groupBy('cache_skipped_reason')
            ->get();

        $out = [];
        foreach ($rows as $r) {
            $key = $r->cache_skipped_reason ?? '(none)';
            $out[$key] = (int) $r->n;
        }

        // Ensure all four documented enum keys are present so the
        // frontend can render zero-valued series without conditional
        // logic. Source of truth for the enum is the CHECK constraint
        // added in database/raw/phase30/10-cache-skipped-reason.sql.
        foreach ([
            'zero_candidates',
            'partial_failures',
            'schema_validation_failed',
            'downhole_bypass_legacy',
            '(none)',
        ] as $k) {
            if (! array_key_exists($k, $out)) {
                $out[$k] = 0;
            }
        }

        return $out;
    }
}
