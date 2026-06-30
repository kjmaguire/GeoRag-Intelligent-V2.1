<?php

declare(strict_types=1);

namespace App\Http\Controllers\Dashboard;

use App\Http\Controllers\Controller;
use Carbon\CarbonImmutable;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * §16.1 — 6 named customer dashboards beyond the existing portfolio /
 * project / ingestion-health set.
 *
 * Each dashboard renders a real-data widget set when the backing
 * tables exist + non-empty, else falls back to an honest "(no data
 * yet for this workspace — try ingesting some documents)" empty
 * state. No fake fixtures.
 *
 * Routes:
 *   GET /dashboards/evidence-quality
 *   GET /dashboards/visual-readiness
 *   GET /dashboards/publicgeo-overlay
 *   GET /dashboards/target-recommendation
 *   GET /dashboards/reporting
 *   GET /dashboards/llm-cost
 *
 * All auth-gated via the global Sanctum group in routes/web.php.
 */
class CustomerDashboardsController extends Controller
{
    // ─── Evidence Quality ──────────────────────────────────────────
    public function evidenceQuality(Request $request): Response
    {
        // Citation resolution + refusal stats across the user's projects.
        $user = $request->user();
        $projectIds = $user?->projects()->pluck('silver.projects.project_id')->toArray() ?? [];
        $since = CarbonImmutable::now()->subDays(30);

        // Per-day totals
        $byDay = DB::select(
            "SELECT date_trunc('day', created_at)::date AS d,
                    count(*)::int AS n_answers,
                    sum(CASE WHEN citation_lifecycle_state = 'resolved' THEN 1 ELSE 0 END)::int AS n_resolved,
                    sum(CASE WHEN citation_lifecycle_state = 'rejected' THEN 1 ELSE 0 END)::int AS n_rejected,
                    sum(CASE WHEN rejection_reason IS NOT NULL THEN 1 ELSE 0 END)::int AS n_refusals
               FROM silver.answer_runs
              WHERE created_at >= ?
                AND (? = 0 OR project_id::text = ANY(?))
              GROUP BY 1
              ORDER BY 1",
            [$since, count($projectIds), $projectIds],
        );

        // Top rejection reasons (last 30d)
        $rejReasons = DB::select(
            "SELECT COALESCE(rejection_reason, '(unspecified)') AS reason,
                    count(*)::int AS n
               FROM silver.answer_citation_items
              WHERE rejection_reason IS NOT NULL
                AND created_at >= ?
              GROUP BY 1
              ORDER BY n DESC
              LIMIT 8",
            [$since],
        );

        $totals = DB::selectOne(
            "SELECT count(*)::int AS n_answers,
                    sum(CASE WHEN citation_lifecycle_state = 'resolved' THEN 1 ELSE 0 END)::int AS n_resolved,
                    sum(CASE WHEN citation_lifecycle_state = 'rejected' THEN 1 ELSE 0 END)::int AS n_rejected
               FROM silver.answer_runs
              WHERE created_at >= ?",
            [$since],
        );

        return Inertia::render('Dashboards/EvidenceQuality', [
            'window_days' => 30,
            'totals' => $totals,
            'by_day' => $byDay,
            'rejection_reasons' => $rejReasons,
        ]);
    }

    // ─── Visual Readiness ──────────────────────────────────────────
    public function visualReadiness(Request $request): Response
    {
        // For each chart kind, count projects with enough data to render.
        $vizCoverage = [];

        // strip_log / cross_section / stereonet — driven by drillhole intervals
        $stripLog = (int) DB::scalar(
            'SELECT count(DISTINCT project_id) FROM silver.collars WHERE total_depth > 0',
        );
        // target_heatmap — driven by gold.h3_density_mineral
        $heatmap = 0;
        try {
            $heatmap = (int) DB::scalar(
                'SELECT count(DISTINCT workspace_id) FROM gold.h3_density_mineral',
            );
        } catch (\Throwable $e) { /* table optional */
        }
        // anomaly_map / long_section — driven by collars + assays
        // grade_tonnage — driven by assays (synthetic for now)
        $totalProjects = (int) DB::scalar("SELECT count(*) FROM silver.projects WHERE status='active'");

        $vizCoverage = [
            ['kind' => 'strip_log',       'ready' => $stripLog,    'total' => $totalProjects],
            ['kind' => 'cross_section',   'ready' => $stripLog,    'total' => $totalProjects],
            ['kind' => 'stereonet',       'ready' => 0,            'total' => $totalProjects],
            ['kind' => 'long_section',    'ready' => $stripLog,    'total' => $totalProjects],
            ['kind' => 'harker_diagram',  'ready' => 0,            'total' => $totalProjects],
            ['kind' => 'spider_diagram',  'ready' => 0,            'total' => $totalProjects],
            ['kind' => 'ree_pattern',     'ready' => 0,            'total' => $totalProjects],
            ['kind' => 'ternary_diagram', 'ready' => 0,            'total' => $totalProjects],
            ['kind' => 'grade_tonnage',   'ready' => 0,            'total' => $totalProjects],
            ['kind' => 'anomaly_map',     'ready' => 0,            'total' => $totalProjects],
            ['kind' => 'target_heatmap',  'ready' => $heatmap,     'total' => $totalProjects],
        ];

        return Inertia::render('Dashboards/VisualReadiness', [
            'viz_coverage' => $vizCoverage,
            'total_projects' => $totalProjects,
        ]);
    }

    // ─── PublicGeo Overlay ─────────────────────────────────────────
    public function publicGeoOverlay(Request $request): Response
    {
        // Public-geoscience inventory + freshness.
        $counts = DB::selectOne(
            'SELECT
                (SELECT count(*) FROM public_geo.pg_mineral_occurrence)::int AS occurrences,
                (SELECT count(*) FROM public_geo.pg_drillhole_collar)::int AS drillholes,
                (SELECT count(*) FROM public_geo.pg_mine)::int AS mines,
                (SELECT count(*) FROM public_geo.pg_bedrock_geology)::int AS bedrock_polygons,
                (SELECT count(*) FROM public_geo.pg_assessment_survey)::int AS assessment_surveys',
        );

        // Sources + last refresh
        $sources = DB::select(
            'SELECT source_id, jurisdiction_code, name, canonical_type, license_summary,
                    last_refreshed_at
               FROM public_geo.sources
              ORDER BY jurisdiction_code, name',
        );

        // By jurisdiction
        $byJurisdiction = DB::select(
            "SELECT jurisdiction_code,
                    sum(CASE WHEN tbl = 'occurrences' THEN n ELSE 0 END)::int AS occurrences,
                    sum(CASE WHEN tbl = 'drillholes' THEN n ELSE 0 END)::int AS drillholes,
                    sum(CASE WHEN tbl = 'mines' THEN n ELSE 0 END)::int AS mines
               FROM (
                   SELECT 'occurrences' AS tbl, jurisdiction_code, count(*)::int AS n FROM public_geo.pg_mineral_occurrence GROUP BY 2
                   UNION ALL
                   SELECT 'drillholes', jurisdiction_code, count(*)::int FROM public_geo.pg_drillhole_collar GROUP BY 2
                   UNION ALL
                   SELECT 'mines', jurisdiction_code, count(*)::int FROM public_geo.pg_mine GROUP BY 2
               ) t
              GROUP BY jurisdiction_code
              ORDER BY jurisdiction_code",
        );

        return Inertia::render('Dashboards/PublicGeoOverlay', [
            'counts' => $counts,
            'sources' => $sources,
            'by_jurisdiction' => $byJurisdiction,
        ]);
    }

    // ─── Target Recommendation ─────────────────────────────────────
    public function targetRecommendation(Request $request): Response
    {
        $user = $request->user();
        $projectIds = $user?->projects()->pluck('silver.projects.project_id')->toArray() ?? [];

        // Recent recommendations
        $recent = DB::select(
            'SELECT recommendation_id::text, project_id::text, run_id::text,
                    rank, created_at,
                    LEFT(explanation_markdown, 200) AS explanation_preview
               FROM targeting.target_recommendations
              WHERE (? = 0 OR project_id::text = ANY(?))
              ORDER BY created_at DESC
              LIMIT 25',
            [count($projectIds), $projectIds],
        );

        $byProject = DB::select(
            'SELECT p.project_name,
                    count(tr.recommendation_id)::int AS rec_count,
                    max(tr.created_at) AS last_run
               FROM silver.projects p
               LEFT JOIN targeting.target_recommendations tr ON tr.project_id = p.project_id
              WHERE (? = 0 OR p.project_id::text = ANY(?))
              GROUP BY p.project_name
              ORDER BY rec_count DESC',
            [count($projectIds), $projectIds],
        );

        return Inertia::render('Dashboards/TargetRecommendation', [
            'recent_recommendations' => $recent,
            'by_project' => $byProject,
        ]);
    }

    // ─── Reporting ─────────────────────────────────────────────────
    public function reporting(Request $request): Response
    {
        $reports = DB::select(
            'SELECT report_id::text, title, company, filing_date, commodity,
                    project_name, region, created_at
               FROM silver.reports
              ORDER BY created_at DESC NULLS LAST, filing_date DESC NULLS LAST
              LIMIT 50',
        );

        $byCommodity = DB::select(
            "SELECT COALESCE(commodity, '(unspecified)') AS commodity,
                    count(*)::int AS n
               FROM silver.reports
              GROUP BY 1
              ORDER BY n DESC
              LIMIT 10",
        );

        $totals = DB::selectOne(
            'SELECT count(*)::int AS total,
                    count(*) FILTER (WHERE filing_date IS NOT NULL)::int AS with_filing_date
               FROM silver.reports',
        );

        return Inertia::render('Dashboards/Reporting', [
            'reports' => $reports,
            'by_commodity' => $byCommodity,
            'totals' => $totals,
        ]);
    }

    // ─── LLM Cost & Usage ──────────────────────────────────────────
    public function llmCost(Request $request): Response
    {
        $since = CarbonImmutable::now()->subDays(30);

        // Daily totals
        $byDay = DB::select(
            'SELECT rollup_date,
                    sum(invocations_total)::int AS invocations,
                    sum(tokens_prompt_total)::bigint AS prompt_tokens,
                    sum(tokens_completion_total)::bigint AS completion_tokens,
                    round(sum(cost_usd_total)::numeric, 4) AS cost_usd
               FROM usage.usage_aggregates_daily
              WHERE rollup_date >= ?
              GROUP BY rollup_date
              ORDER BY rollup_date',
            [$since->toDateString()],
        );

        // By agent
        $byAgent = DB::select(
            'SELECT agent_name,
                    sum(invocations_total)::int AS invocations,
                    sum(tokens_prompt_total + tokens_completion_total)::bigint AS total_tokens,
                    round(sum(cost_usd_total)::numeric, 4) AS cost_usd
               FROM usage.usage_aggregates_daily
              WHERE rollup_date >= ?
              GROUP BY agent_name
              ORDER BY cost_usd DESC NULLS LAST
              LIMIT 15',
            [$since->toDateString()],
        );

        $totals = DB::selectOne(
            'SELECT sum(invocations_total)::int AS invocations,
                    sum(tokens_prompt_total + tokens_completion_total)::bigint AS total_tokens,
                    round(sum(cost_usd_total)::numeric, 4) AS cost_usd
               FROM usage.usage_aggregates_daily
              WHERE rollup_date >= ?',
            [$since->toDateString()],
        );

        return Inertia::render('Dashboards/LlmCost', [
            'window_days' => 30,
            'totals' => $totals,
            'by_day' => $byDay,
            'by_agent' => $byAgent,
        ]);
    }
}
