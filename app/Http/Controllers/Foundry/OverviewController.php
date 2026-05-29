<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\QueryAuditLog;
use Carbon\CarbonImmutable;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/OverviewController — the project landing dashboard.
 *
 * Lands at /projects/{slug} (no subpath). Summarises everything project-scoped:
 * collar count, sample count, recent queries, hypotheses count, ingest health,
 * and the recommended next action.
 */
class OverviewController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $collarCount = DB::table('silver.collars')->where('project_id', $project->project_id)->count();
        $totalMeters = (int) (DB::table('silver.collars')->where('project_id', $project->project_id)->sum('total_depth') ?? 0);
        $sampleCount = (int) DB::table('silver.samples as s')
            ->join('silver.collars as c', 's.collar_id', '=', 'c.collar_id')
            ->where('c.project_id', $project->project_id)
            ->count();

        $logCurveCount = 0;
        try {
            $logCurveCount = (int) DB::table('silver.well_log_curves as w')
                ->join('silver.collars as c', 'w.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $project->project_id)
                ->count();
        } catch (\Throwable $e) { /* schema drift */
        }

        $hypothesesCount = 0;
        try {
            $hypothesesCount = DB::table('silver.hypotheses')->where('project_id', $project->project_id)->count();
        } catch (\Throwable $e) { /* */
        }

        $reportsCount = 0;
        try {
            $reportsCount = DB::table('silver.reports')->count();
        } catch (\Throwable $e) { /* */
        }

        // Ingest summary — counts files in MinIO under bronze/reports/{project_id}/
        // that don't yet have a silver.reports row (fuzzy filename match), so
        // the Overview can show an "X files ingesting" card linking to the
        // dedicated Ingestion Runs page. Cheap because the bucket is partitioned
        // by project_id and a single project rarely has more than a few dozen
        // PDFs in flight at once.
        $ingestSummary = $this->buildIngestSummary($project->project_id);

        $sinceDay = CarbonImmutable::now()->subDay();
        $queries24h = QueryAuditLog::where('project_id', $project->project_id)->where('created_at', '>=', $sinceDay)->count();
        $queries7d = QueryAuditLog::where('project_id', $project->project_id)->where('created_at', '>=', CarbonImmutable::now()->subDays(7))->count();
        $avgConf = (float) (QueryAuditLog::where('project_id', $project->project_id)->whereNotNull('response_text')->avg('confidence') ?? 0);

        $recentActivity = QueryAuditLog::where('project_id', $project->project_id)
            ->orderByDesc('created_at')
            ->limit(12)
            ->get()
            ->map(fn ($r) => [
                'id' => (string) $r->id,
                'when' => $r->created_at?->diffForHumans() ?? '—',
                'kind' => $r->response_text ? 'query' : 'refusal',
                'text' => substr((string) ($r->query_text ?? ''), 0, 120),
            ])->values();

        // Recommended next action — picks the highest-leverage thing to do based
        // on the project's current state.
        $nextAction = match (true) {
            $collarCount === 0 => ['title' => 'Connect your first data source', 'detail' => 'Upload drill logs or ingest the Wyoming WSGS archive to start the corpus.', 'cta' => 'Open import wizard', 'href' => '/foundry/imports/wizard'],
            $queries7d === 0 => ['title' => 'Ask your first hypothesis', 'detail' => 'The chat is the main interface — pin sources, rank candidates, save runs.', 'cta' => 'Open Chat', 'href' => "/projects/{$slug}/chat"],
            $hypothesesCount === 0 => ['title' => 'Explore the reasoning workbench', 'detail' => 'Evidence → Reasoning → Candidates → Evidence Graph.', 'cta' => 'Open Reasoning', 'href' => "/projects/{$slug}/reasoning"],
            $reportsCount === 0 => ['title' => 'Draft a recommendation report', 'detail' => 'Block editor with live citations + version diff.', 'cta' => 'Open Reports', 'href' => "/projects/{$slug}/reports"],
            default => ['title' => 'Inspect target recommendations', 'detail' => 'See the latest ranked drill targets with SHAP feature weights.', 'cta' => 'Open Targets', 'href' => "/projects/{$slug}/targets"],
        };

        return Inertia::render('Foundry/Overview', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
                'region' => $project->region,
                'commodity' => $project->commodity,
                'status' => is_object($project->status) ? $project->status->value : ($project->status ?? 'active'),
                'crs_epsg' => $project->crs_epsg,
                'data_version' => $project->data_version ?? 0,
            ],
            'kpis' => [
                ['label' => 'COLLARS', 'value' => (string) $collarCount, 'sub' => number_format($totalMeters).' m drilled'],
                ['label' => 'SAMPLES', 'value' => (string) $sampleCount],
                ['label' => 'LOG CURVES', 'value' => (string) $logCurveCount, 'sub' => 'gamma + grade + lithology'],
                ['label' => 'HYPOTHESES', 'value' => (string) $hypothesesCount, 'sub' => 'in reasoning workbench'],
                ['label' => 'QUERIES · 7D', 'value' => (string) $queries7d, 'sub' => "{$queries24h} in 24h", 'tone' => 'accent'],
                ['label' => 'AVG CONFIDENCE', 'value' => number_format($avgConf, 2), 'sub' => 'across answered queries'],
            ],
            'next_action' => $nextAction,
            'recent_activity' => $recentActivity,
            'ingest_summary' => $ingestSummary,
            'empty' => $collarCount === 0 && $queries7d === 0,
        ]);
    }

    /**
     * Lightweight in-flight count for the Overview card. Mirrors the matching
     * logic in IngestionRunsController but only returns the totals + the most
     * recent in-flight filename, so the Overview render stays cheap.
     *
     * @return array{in_flight: int, completed: int, latest_in_flight: ?string}
     */
    private function buildIngestSummary(string $projectId): array
    {
        $reportTitles = [];
        try {
            $reportTitles = DB::table('silver.reports')
                ->where('project_id', $projectId)
                ->pluck('title')
                ->all();
        } catch (\Throwable $e) {
            // empty
        }

        $titleFps = [];
        foreach ($reportTitles as $t) {
            $fp = $this->fingerprint((string) $t);
            if ($fp !== '') {
                $titleFps[$fp] = true;
            }
        }

        $inFlight = 0;
        $latest = null;
        $latestMtime = 0;

        try {
            $disk = Storage::disk('s3-bronze');
            foreach (['reports', 'tiff'] as $prefix) {
                foreach ($disk->files("{$prefix}/{$projectId}") as $key) {
                    $filename = basename($key);
                    $stem = pathinfo($filename, PATHINFO_FILENAME);
                    $stem = preg_replace('/^\d{8}_\d{6}_/', '', $stem) ?? $stem;
                    $fp = $this->fingerprint($stem);

                    $matched = false;
                    foreach ($titleFps as $titleFp => $_) {
                        if (str_starts_with($fp, $titleFp)) {
                            $matched = true;
                            break;
                        }
                    }
                    if ($matched) {
                        continue;
                    }

                    $inFlight++;
                    try {
                        $mtime = $disk->lastModified($key);
                    } catch (\Throwable $e) {
                        $mtime = 0;
                    }
                    if ($mtime >= $latestMtime) {
                        $latestMtime = $mtime;
                        $latest = $filename;
                    }
                }
            }
        } catch (\Throwable $e) {
            // bucket may be unreachable — degrade silently
        }

        $completed = count($reportTitles);

        return [
            'in_flight' => $inFlight,
            'completed' => $completed,
            'latest_in_flight' => $latest,
        ];
    }

    private function fingerprint(string $value): string
    {
        $alnum = preg_replace('/[^a-z0-9]+/', '', strtolower($value)) ?? '';

        return substr($alnum, 0, 40);
    }
}
