<?php

declare(strict_types=1);

namespace App\Http\Controllers\Dashboard;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\QueryAuditLog;
use Carbon\CarbonImmutable;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

/**
 * Portfolio-level dashboard endpoints.
 *
 * Widgets that *have* a real data source (project counts, query audit log,
 * collars, document ingestion rows) are wired directly against the DB.
 * Widgets that depend on tables we don't yet have (user-feedback ratings,
 * per-stage ingestion event log at the doc level) return an empty-shape
 * response that the React client already handles ("0 queries", "no
 * feedback yet"). No more placeholder Tintina / Talbot / Eastmain names.
 */
class PortfolioController extends Controller
{
    public function platformReadiness(Request $request): JsonResponse
    {
        // Platform-readiness is service health, not data — keep the fixture
        // until we have a real service health polling layer (tracked in M2).
        return $this->fixture('platform-readiness');
    }

    public function kpis(Request $request): JsonResponse
    {
        $user = $request->user();
        $projectIds = $user->projects()->pluck('silver.projects.project_id');

        $activeCount = Project::whereIn('project_id', $projectIds)
            ->where(function ($q) {
                $q->where('status', 'active')->orWhereNull('status');
            })->count();
        $archivedCount = Project::whereIn('project_id', $projectIds)
            ->where('status', 'archived')->count();

        // Documents indexed = sum of collars across the user's projects
        // (until a real "documents" table is introduced this is the
        // closest honest proxy we have for indexed artifact count).
        $docsIndexed = DB::table('silver.collars')
            ->whereIn('project_id', $projectIds)
            ->count();

        $since = CarbonImmutable::now()->subDays(7);
        $docsThisWeek = DB::table('silver.collars')
            ->whereIn('project_id', $projectIds)
            ->where('created_at', '>=', $since)
            ->count();

        // Query metrics pulled straight from the audit log.
        $queries24h = QueryAuditLog::where('user_id', $user->id)
            ->whereIn('project_id', $projectIds)
            ->where('created_at', '>=', CarbonImmutable::now()->subDay())
            ->count();

        $thirtyDayAvgPerDay = (int) round(
            QueryAuditLog::where('user_id', $user->id)
                ->whereIn('project_id', $projectIds)
                ->where('created_at', '>=', CarbonImmutable::now()->subDays(30))
                ->count() / 30,
        );

        // Citation resolution rate = fraction of answered queries that
        // have non-empty citations. Only queries with a response_text
        // (i.e. the Horizon job completed) are counted.
        $answered = QueryAuditLog::where('user_id', $user->id)
            ->whereIn('project_id', $projectIds)
            ->whereNotNull('response_text')
            ->where('created_at', '>=', CarbonImmutable::now()->subDays(7));
        $answeredCount = (clone $answered)->count();
        $citedCount = (clone $answered)
            ->whereRaw('citations IS NOT NULL AND jsonb_array_length(citations) > 0')
            ->count();
        $citationRate = $answeredCount > 0 ? round($citedCount / $answeredCount, 3) : null;

        // Contract: match the fixture shape exactly (the React tiles are
        // keyed off dotted paths, not a reshaped payload).
        return response()->json([
            'data' => [
                'projects' => ['active' => $activeCount, 'archived' => $archivedCount],
                'documents_indexed' => $docsIndexed,
                'queries_24h' => $queries24h,
                'citation_resolution_rate' => $citationRate,
                'feedback_signal' => null,  // awaits M2 §10p feedback capture
                'trends' => [
                    'documents_indexed_7d_delta' => $docsThisWeek,
                    'queries_24h_vs_avg_pct' => $thirtyDayAvgPerDay > 0
                        ? round((($queries24h - $thirtyDayAvgPerDay) / $thirtyDayAvgPerDay) * 100, 1)
                        : null,
                    'citation_resolution_wow_delta' => null,  // need week-over-week baseline
                ],
            ],
            'generated_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => 60,
        ]);
    }

    public function projects(Request $request): JsonResponse
    {
        $user = $request->user();
        $projectIds = $user->projects()->pluck('silver.projects.project_id');

        // queries_7d per project in one shot.
        $since = CarbonImmutable::now()->subDays(7);
        $queryCountsByProject = QueryAuditLog::selectRaw('project_id, COUNT(*) AS c')
            ->where('user_id', $user->id)
            ->whereIn('project_id', $projectIds)
            ->where('created_at', '>=', $since)
            ->groupBy('project_id')
            ->pluck('c', 'project_id');

        $projects = Project::whereIn('project_id', $projectIds)
            ->withCount('collars')
            ->orderBy('updated_at', 'desc')
            ->get();

        $rows = $projects->map(function (Project $p) use ($queryCountsByProject) {
            return [
                'id' => $p->project_id,
                'slug' => $p->slug ?? Str::slug($p->project_name),
                'name' => $p->project_name,
                'status' => $p->status?->value ?? 'active',
                'region' => $p->region ?? '—',
                'doc_count' => $p->collars_count ?? 0,
                'queries_7d' => (int) ($queryCountsByProject[$p->project_id] ?? 0),
                'last_activity_at' => $p->updated_at?->toIso8601String(),
                'last_activity_kind' => null,
            ];
        });

        return response()->json([
            'data' => $rows,
            'generated_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    public function queryActivity(Request $request): JsonResponse
    {
        $user = $request->user();
        $projectIds = $user->projects()->pluck('silver.projects.project_id');

        $since = CarbonImmutable::now()->subDays(14)->startOfDay();
        $rows = QueryAuditLog::selectRaw('DATE(created_at) AS day, COUNT(*) AS c')
            ->where('user_id', $user->id)
            ->whereIn('project_id', $projectIds)
            ->where('created_at', '>=', $since)
            ->groupBy('day')
            ->orderBy('day')
            ->get();

        // Zero-fill missing days so the sparkline has a continuous x-axis.
        $byDay = $rows->keyBy(fn ($r) => (string) $r->day);
        $points = [];
        $day = $since;
        $today = CarbonImmutable::now()->startOfDay();
        while ($day <= $today) {
            $k = $day->format('Y-m-d');
            $points[] = [
                'date' => $k,
                'count' => (int) ($byDay[$k]->c ?? 0),
            ];
            $day = $day->addDay();
        }

        $todayCount = end($points)['count'] ?? 0;
        $windowAvg = count($points) > 1
            ? (int) round(array_sum(array_column(array_slice($points, 0, -1), 'count')) / (count($points) - 1))
            : 0;

        return response()->json([
            'data' => [
                'points' => $points,
                'today_count' => $todayCount,
                'window_avg' => $windowAvg,
            ],
            'generated_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    public function ingestionHealth(Request $request): JsonResponse
    {
        // Per-stage ingestion counts per project. We don't have a
        // document-level stage ledger yet (that's a Dagster-backed table
        // in M2 — see §05 in the arch doc), so we approximate with what
        // IS queryable: collars per project act as the "indexed" count,
        // with everything else at zero. The UI can render a sparser
        // bar chart gracefully.
        $user = $request->user();
        $projectIds = $user->projects()->pluck('silver.projects.project_id');

        $projects = Project::whereIn('project_id', $projectIds)
            ->withCount('collars')
            ->orderBy('updated_at', 'desc')
            ->get();

        $rows = $projects->map(fn (Project $p) => [
            'project_id' => $p->project_id,
            'project_name' => $p->project_name,
            'bronze' => 0,
            'silver' => 0,
            'gold' => 0,
            'index' => $p->collars_count ?? 0,  // fixture-compat key name
            'failed' => 0,
            'total' => $p->collars_count ?? 0,
        ]);

        return response()->json([
            'data' => $rows,
            'generated_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    public function feedback(Request $request): JsonResponse
    {
        // Real feedback ratings are collected via a user-feedback table
        // that ships with M2 §10p. Return an honest empty state so the
        // UI can render an "awaiting feedback" widget rather than a
        // fabricated 78%.
        return response()->json([
            'data' => [
                'total' => 0,
                'helpful' => 0,
                'citation_issue' => 0,
                'wrong_irrelevant' => 0,
                'positive_rate' => null,
                'top_issue' => null,
            ],
            'generated_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => 900,
        ]);
    }

    public function activity(Request $request): JsonResponse
    {
        // Real cross-project activity feed — latest query events. Ingest /
        // CRS detection / KG event feeds live in M2; until then only the
        // `query` kind is populated, which beats showing fake "Talbot
        // Lake" rows.
        $user = $request->user();
        $projectIds = $user->projects()->pluck('silver.projects.project_id');
        $projectNamesById = Project::whereIn('project_id', $projectIds)
            ->pluck('project_name', 'project_id');

        $rows = QueryAuditLog::where('user_id', $user->id)
            ->whereIn('project_id', $projectIds)
            ->orderByDesc('created_at')
            ->limit(10)
            ->get();

        $items = $rows->map(function (QueryAuditLog $r) use ($projectNamesById) {
            $citationCount = is_array($r->citations) ? count($r->citations) : 0;
            $summary = $r->response_text !== null
                ? "Query resolved with {$citationCount} citation".($citationCount === 1 ? '' : 's')
                    .' — "'.Str::limit($r->query_text, 80).'"'
                : 'Query in flight — "'.Str::limit($r->query_text, 80).'"';

            return [
                'id' => $r->audit_id,
                'occurred_at' => $r->created_at?->toIso8601String(),
                'project_id' => $r->project_id,
                'project_name' => $projectNamesById[$r->project_id] ?? 'Unknown project',
                'kind' => 'query',
                'summary' => $summary,
                'detail_ref' => "qry-{$r->query_id}",
            ];
        });

        return response()->json([
            'data' => $items,
            'generated_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => 60,
        ]);
    }

    private function fixture(string $name): JsonResponse
    {
        $json = $this->loadFixture($name);

        return response()->json($json);
    }

    private function loadFixture(string $name): array
    {
        $path = database_path("fixtures/dashboard/{$name}.json");

        if (! file_exists($path)) {
            return ['data' => null, 'generated_at' => now()->toIso8601String(), 'cache_ttl_seconds' => 0];
        }

        $json = json_decode(file_get_contents($path), true);
        $json['generated_at'] = now()->toIso8601String();

        return $json;
    }
}
