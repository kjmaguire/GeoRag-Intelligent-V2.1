<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\QueryAuditLog;
use Carbon\CarbonImmutable;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/ProjectAnalyticsController — per-project deep-dive with RAG-quality
 * time series + drill-economics + corpus composition + refusal-by-gate.
 */
class ProjectAnalyticsController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $days = (int) ($request->query('days') ?? 90);
        $since = CarbonImmutable::now()->subDays($days);

        $totalQueries = QueryAuditLog::where('project_id', $project->project_id)
            ->where('created_at', '>=', $since)
            ->count();
        $refusedQueries = QueryAuditLog::where('project_id', $project->project_id)
            ->where('created_at', '>=', $since)
            ->whereNull('response_text')
            ->count();
        $avgConfidence = (float) (QueryAuditLog::where('project_id', $project->project_id)
            ->where('created_at', '>=', $since)
            ->whereNotNull('response_text')
            ->avg('confidence') ?? 0);

        // Refusal-by-week stacked bar — synthesised from null/non-null response_text only
        // because the schema doesn't carry an explicit gate column.
        $weeks = [];
        for ($i = 11; $i >= 0; $i--) {
            $weekStart = CarbonImmutable::now()->subWeeks($i)->startOfWeek();
            $weekEnd = $weekStart->endOfWeek();
            $refused = QueryAuditLog::where('project_id', $project->project_id)
                ->whereBetween('created_at', [$weekStart, $weekEnd])
                ->whereNull('response_text')
                ->count();
            $weeks[] = [
                'week' => $weekStart->format('W'),
                'gates' => [
                    'g6_calibration' => $refused, // bucket all refused into g6 until typed
                    'g4_citation_anchor' => 0,
                    'g2_route_oos' => 0,
                    'g5_typed_output' => 0,
                    'g1_safety' => 0,
                ],
            ];
        }

        // Confidence histogram — 20 bins
        $bins = array_fill(0, 20, ['low' => 0.0, 'high' => 0.0, 'count' => 0]);
        for ($b = 0; $b < 20; $b++) {
            $bins[$b]['low'] = $b * 0.05;
            $bins[$b]['high'] = ($b + 1) * 0.05;
        }
        $confidences = QueryAuditLog::where('project_id', $project->project_id)
            ->where('created_at', '>=', $since)
            ->whereNotNull('confidence')
            ->pluck('confidence');
        foreach ($confidences as $c) {
            $idx = min(19, max(0, (int) floor(((float) $c) * 20)));
            $bins[$idx]['count']++;
        }

        // Drill economics summary
        $collarsTotal = DB::table('silver.collars')->where('project_id', $project->project_id)->count();
        $totalMeters = (int) (DB::table('silver.collars')->where('project_id', $project->project_id)->sum('total_depth') ?? 0);
        // silver.samples lacks project_id — join through silver.collars.
        $samplesCount = DB::table('silver.samples as s')
            ->join('silver.collars as c', 's.collar_id', '=', 'c.collar_id')
            ->where('c.project_id', $project->project_id)
            ->count();

        // Top queries by frequency (group by query_text_hash)
        $topQueries = QueryAuditLog::where('project_id', $project->project_id)
            ->where('created_at', '>=', $since)
            ->select('query_text', DB::raw('COUNT(*) as freq'))
            ->whereNotNull('query_text')
            ->groupBy('query_text')
            ->orderByDesc('freq')
            ->limit(10)
            ->get();

        return Inertia::render('Foundry/ProjectAnalytics', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
                'region' => $project->region,
                'commodity' => $project->commodity,
            ],
            'window_days' => $days,
            'kpis' => [
                ['label' => 'QUERIES', 'value' => (string) $totalQueries, 'sub' => "{$days}d window", 'tone' => 'accent'],
                ['label' => 'REFUSED', 'value' => (string) $refusedQueries, 'sub' => $totalQueries > 0 ? round(($refusedQueries / $totalQueries) * 100, 1) . '%' : '0%'],
                ['label' => 'AVG CONFIDENCE', 'value' => number_format($avgConfidence, 2)],
                ['label' => 'COLLARS', 'value' => (string) $collarsTotal, 'sub' => number_format($totalMeters) . ' m total'],
                ['label' => 'SAMPLES', 'value' => (string) $samplesCount],
            ],
            'refusal_by_week' => $weeks,
            'confidence_histogram' => array_values($bins),
            'top_queries' => $topQueries->map(fn ($q) => [
                'text' => substr((string) $q->query_text, 0, 120),
                'freq' => (int) $q->freq,
            ])->values(),
            'empty' => $totalQueries === 0 && $collarsTotal === 0,
        ]);
    }
}
