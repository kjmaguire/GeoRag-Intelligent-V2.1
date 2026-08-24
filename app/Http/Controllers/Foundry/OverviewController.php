<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\QueryAuditLog;
use App\Services\StorageService;
use App\Support\SetsWorkspaceRlsContext;
use Carbon\CarbonImmutable;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
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
    use SetsWorkspaceRlsContext;

    public function __construct(
        private readonly StorageService $storage,
    ) {}

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

        $reportsCount = 0;
        try {
            // Must be project-scoped like every other count above. This was an
            // unfiltered count(*) over silver.reports, i.e. every report in the
            // database regardless of project — it only looked right because
            // this deployment currently has a single project. A second project
            // would show the first project's documents in its own Overview, and
            // the $nextAction match below would mis-fire ("Connect your first
            // data source" is gated on $reportsCount === 0, which an unscoped
            // count can never reach once any project has ingested anything).
            $reportsCount = DB::table('silver.reports')
                ->where('project_id', $project->project_id)
                ->count();
        } catch (\Throwable $e) { /* */
        }

        // Ingest summary — counts files in MinIO under bronze/reports/{project_id}/
        // that don't yet have a silver.reports row (fuzzy filename match), so
        // the Overview can show an "X files ingesting" card linking to the
        // dedicated Ingestion Runs page. Cheap because the bucket is partitioned
        // by project_id and a single project rarely has more than a few dozen
        // PDFs in flight at once.
        $ingestSummary = $this->buildIngestSummary($project->project_id);

        // OCR corpus coverage. See ocrCoverage()'s docblock for what this
        // number is and, more importantly, what it is not.
        $ocrCoverage = $this->ocrCoverage(
            (string) $project->project_id,
            (string) $project->workspace_id,
        );

        $sinceDay = CarbonImmutable::now()->subDay();
        $queries24h = QueryAuditLog::where('project_id', $project->project_id)->where('created_at', '>=', $sinceDay)->count();
        $queries7d = QueryAuditLog::where('project_id', $project->project_id)->where('created_at', '>=', CarbonImmutable::now()->subDays(7))->count();
        // Null when nothing has been answered yet, and deliberately not
        // coalesced to 0.0: "0.00 avg confidence" on a project that has
        // never been asked a question reads as an appalling score rather
        // than as no data. The KPI renders an em dash for null.
        $rawAvgConf = QueryAuditLog::where('project_id', $project->project_id)
            ->whereNotNull('response_text')
            ->avg('confidence');
        $avgConf = $rawAvgConf !== null ? (float) $rawAvgConf : null;

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
            // "Connect your first source" only when the project is genuinely
            // empty — a document corpus without drill data is a working
            // project (chat answers from reports), not a cold start. Keying
            // on collars alone told users with 7 ingested reports to
            // "connect your first data source".
            $collarCount === 0 && $reportsCount === 0 => ['title' => 'Connect your first data source', 'detail' => 'Upload drill logs or ingest the Wyoming WSGS archive to start the corpus.', 'cta' => 'Open import wizard', 'href' => '/foundry/imports/wizard'],
            $queries7d === 0 => ['title' => 'Ask your first hypothesis', 'detail' => 'The chat is the main interface — pin sources, rank candidates, save runs.', 'cta' => 'Open Chat', 'href' => "/projects/{$slug}/chat"],
            // Drill data, queries being asked, and nothing for an answer to
            // cite. The old copy here — "Draft a recommendation report /
            // Block editor with live citations + version diff" — described
            // the admin report builder, which has no page, and sent the user
            // to the ingested-filings reader instead. Wrong feature, wrong
            // destination, and not the highest-leverage move either: without
            // documents the chat has nothing to ground an answer in.
            $reportsCount === 0 => ['title' => 'Add technical reports', 'detail' => 'This project has drill data but no documents. Chat can only cite what has been ingested.', 'cta' => 'Open import wizard', 'href' => '/foundry/imports/wizard'],
            // /corpus is a 302 to /reports (merged 2026-08-18). Linking the
            // redirect costs a round-trip and names a "Reader" page that no
            // longer exists.
            default => ['title' => 'Review your document corpus', 'detail' => 'Read ingested passages and open their source filings.', 'cta' => 'Open Reports', 'href' => "/projects/{$slug}/reports"],
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
                // 'gamma + grade + lithology' was a hardcoded claim about
                // what this project's LAS files contain. It is true of the
                // Cameco corpus and of nothing else in particular.
                ['label' => 'LOG CURVES', 'value' => (string) $logCurveCount, 'sub' => 'downhole curves'],
                ['label' => 'REPORTS', 'value' => (string) $reportsCount, 'sub' => 'in document corpus'],
                ['label' => 'QUERIES · 7D', 'value' => (string) $queries7d, 'sub' => "{$queries24h} in 24h", 'tone' => 'accent'],
                [
                    'label' => 'AVG CONFIDENCE',
                    'value' => $avgConf !== null ? number_format($avgConf, 2) : '—',
                    'sub' => $avgConf !== null ? 'across answered queries' : 'no answers yet',
                ],
            ],
            'next_action' => $nextAction,
            'recent_activity' => $recentActivity,
            'ingest_summary' => $ingestSummary,
            'ocr_coverage' => $ocrCoverage,
            'empty' => $collarCount === 0 && $queries7d === 0,
        ]);
    }

    /**
     * OCR corpus coverage for this project, by engine and router verdict.
     *
     * WHAT THIS ANSWERS
     *   How much of the corpus came out of an OCR engine rather than a PDF
     *   text layer, and how much of that the ingest-side quality router
     *   flagged as low confidence.
     *
     * WHAT IT DOES NOT ANSWER
     *   Whether the OCR is CORRECT. Nothing in this repo can answer that
     *   yet: there is no ground-truth page set and no CER/WER harness, so
     *   engine choice, DPI and the routing thresholds are all unmeasured.
     *   The card wording must not imply an accuracy figure — a geologist
     *   reading "98% OCR coverage" as "98% accurate" is exactly the
     *   misreading this method exists to replace.
     *
     * THREE SCOPING DECISIONS, ALL DELIBERATE
     *   1. Joined through silver.reports. document_passages is keyed by
     *      document_id (= report_id) and workspace_id; it carries no
     *      project_id of its own.
     *   2. Runs inside withWorkspaceRls. silver.document_passages is
     *      fail-closed, so without the GUC this returns zero rows and the
     *      card reads "no corpus" for a project with thousands of passages.
     *   3. modality='image' rows are excluded. Those are a vision model's
     *      DESCRIPTION of a rendered page — no OCR ran on them, so counting
     *      them would dilute the denominator with pages that were never
     *      candidates for OCR in the first place.
     *
     * A NULL ocr_method gets its own bucket rather than being folded into a
     * guess: passages predating the 2026-05-22 column exist, and calling
     * them "native" would overstate how much of the corpus skipped OCR.
     *
     * @return array{total: int, ocr_total: int, native_total: int, unknown_total: int, flagged_total: int, by_method: array<int, array{method: string, label: string, is_ocr: bool, count: int, flagged: int}>, measured_accuracy: null}
     */
    private function ocrCoverage(string $projectId, string $workspaceId): array
    {
        /** Engines that read a text layer. Everything else ran OCR. */
        $native = ['fitz_native', 'pdfplumber_native'];

        $labels = [
            'fitz_native' => 'Text layer (pdfium)',
            'pdfplumber_native' => 'Text layer (pdfplumber)',
            'tesseract' => 'Tesseract OCR',
            'document_intelligence' => 'Document Intelligence',
            'unavailable' => 'No engine available',
            'unknown' => 'Not recorded',
        ];

        try {
            $rows = $this->withWorkspaceRls($workspaceId, fn () => DB::table('silver.document_passages as dp')
                ->join('silver.reports as r', 'r.report_id', '=', 'dp.document_id')
                ->where('r.project_id', $projectId)
                ->where('dp.modality', '!=', 'image')
                ->select(
                    DB::raw("COALESCE(dp.ocr_method, 'unknown') as method"),
                    DB::raw('count(*)::int as n'),
                    DB::raw("count(*) FILTER (WHERE dp.ocr_status = 'low_confidence')::int as flagged"),
                )
                ->groupBy('method')
                ->get());
        } catch (\Throwable $e) { /* schema drift — the card simply does not render */
            return [
                'total' => 0, 'ocr_total' => 0, 'native_total' => 0,
                'unknown_total' => 0, 'flagged_total' => 0,
                'by_method' => [], 'measured_accuracy' => null,
            ];
        }

        $byMethod = [];
        $total = $ocrTotal = $nativeTotal = $unknownTotal = $flaggedTotal = 0;

        foreach ($rows as $row) {
            $method = (string) $row->method;
            $count = (int) $row->n;
            $flagged = (int) $row->flagged;
            $isOcr = $method !== 'unknown' && ! in_array($method, $native, true);

            $total += $count;
            $flaggedTotal += $flagged;
            if ($method === 'unknown') {
                $unknownTotal += $count;
            } elseif ($isOcr) {
                $ocrTotal += $count;
            } else {
                $nativeTotal += $count;
            }

            $byMethod[] = [
                'method' => $method,
                'label' => $labels[$method] ?? $method,
                'is_ocr' => $isOcr,
                'count' => $count,
                'flagged' => $flagged,
            ];
        }

        usort($byMethod, fn ($a, $b) => $b['count'] <=> $a['count']);

        return [
            'total' => $total,
            'ocr_total' => $ocrTotal,
            'native_total' => $nativeTotal,
            'unknown_total' => $unknownTotal,
            'flagged_total' => $flaggedTotal,
            'by_method' => $byMethod,
            // Explicitly null, and typed that way, so a consumer cannot
            // mistake coverage for accuracy. It becomes a number when a
            // ground-truth page set and a CER/WER harness exist (L776a).
            'measured_accuracy' => null,
        ];
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
            $disk = $this->storage->bronzeReadOnly();
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
