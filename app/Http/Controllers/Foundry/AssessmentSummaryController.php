<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\FastApiJwtMinter;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * CC-01 Item 5 — Foundry/AssessmentSummaryController.
 *
 * Inertia surface for the structured, source-cited summary of an ingested
 * assessment report. Reads the cached summary from
 * silver.assessment_report_summaries; the "Regenerate" button on the page
 * POSTs to the FastAPI summariser via the JWT bridge.
 *
 *   GET  /projects/{slug}/reports/{report_id}/assessment-summary
 *   POST /projects/{slug}/reports/{report_id}/assessment-summary/regenerate
 *
 * The summary is keyed on (workspace_id, pdf_id, model_id) — workspace_id
 * resolves from the project, pdf_id resolves from the report's bronze
 * source-file hash.
 */
class AssessmentSummaryController extends Controller
{
    use SetsWorkspaceRlsContext;

    public function show(Request $request, string $slug, string $reportId): Response
    {
        [$project, $report, $workspaceId] = $this->resolveContext($request, $slug, $reportId);

        $pdfId = $this->resolvePdfIdForReport((string) $report->report_id);

        $summary = $pdfId !== null
            ? DB::table('silver.assessment_report_summaries')
                ->where('workspace_id', $workspaceId)
                ->where('pdf_id', $pdfId)
                ->orderByDesc('generated_at')
                ->first()
            : null;

        $completenessAudit = $pdfId !== null
            ? $this->loadLatestCompletenessAudit($workspaceId, $pdfId)
            : null;

        return Inertia::render('Foundry/AssessmentSummary', [
            'project' => [
                'project_id' => (string) $project->project_id,
                'project_name' => (string) $project->project_name,
                'slug' => (string) $project->slug,
            ],
            'report' => [
                'report_id' => (string) $report->report_id,
                'title' => (string) ($report->title ?? 'Untitled report'),
                'company' => (string) ($report->company ?? ''),
                'filing_date' => (string) ($report->filing_date ?? ''),
                'commodity' => (string) ($report->commodity ?? ''),
                'pdf_id' => $pdfId,
            ],
            'summary' => $summary === null ? null : [
                'summary_id' => (string) $summary->summary_id,
                'sections' => $this->decodeJsonb($summary->sections),
                'completeness_checklist' => $this->decodeJsonb($summary->completeness_checklist),
                'mean_claim_confidence' => $summary->mean_claim_confidence !== null
                    ? (float) $summary->mean_claim_confidence
                    : null,
                'model_id' => (string) $summary->model_id,
                'model_backend' => (string) $summary->model_backend,
                'generated_at' => (string) $summary->generated_at,
            ],
            'can_regenerate' => $pdfId !== null,
            'completeness_audit' => $completenessAudit,
        ]);
    }

    public function regenerate(Request $request, string $slug, string $reportId): JsonResponse
    {
        [$project, $report, $workspaceId] = $this->resolveContext($request, $slug, $reportId);

        $pdfId = $this->resolvePdfIdForReport((string) $report->report_id);
        if ($pdfId === null) {
            return response()->json(
                ['error' => 'No bronze PDF linked to this report — cannot regenerate.'],
                422,
            );
        }

        $fastApiBase = rtrim(
            (string) (config('services.fastapi.internal_url')
                ?? env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000')),
            '/',
        );
        $serviceKey = config('services.fastapi.service_key') ?? env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            return response()->json(['error' => 'FastAPI service key not configured.'], 503);
        }

        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $request->user()->id,
            (string) $project->project_id,
            roles: [],
            workspaceId: $workspaceId,
        );

        $resp = Http::withHeaders([
            'X-Service-Key' => $serviceKey,
            'Authorization' => 'Bearer '.$jwt,
            'Accept' => 'application/json',
        ])->timeout(300)->post(
            $fastApiBase.'/assessment_summary/'.$pdfId,
            ['force_regenerate' => true],
        );

        if (! $resp->ok()) {
            return response()->json(
                ['error' => 'FastAPI summariser returned HTTP '.$resp->status(), 'detail' => $resp->json()],
                502,
            );
        }

        return response()->json($resp->json());
    }

    public function runCompletenessAudit(Request $request, string $slug, string $reportId): JsonResponse
    {
        [$project, $report, $workspaceId] = $this->resolveContext($request, $slug, $reportId);

        $pdfId = $this->resolvePdfIdForReport((string) $report->report_id);
        if ($pdfId === null) {
            return response()->json(
                ['error' => 'No bronze PDF linked to this report — cannot run audit.'],
                422,
            );
        }

        $fastApiBase = rtrim(
            (string) (config('services.fastapi.internal_url')
                ?? env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000')),
            '/',
        );
        $serviceKey = config('services.fastapi.service_key') ?? env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            return response()->json(['error' => 'FastAPI service key not configured.'], 503);
        }

        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $request->user()->id,
            (string) $project->project_id,
            roles: [],
            workspaceId: $workspaceId,
        );

        $url = $fastApiBase.'/completeness_audit/'.$pdfId
            .'?project_id='.urlencode((string) $project->project_id);

        $resp = Http::withHeaders([
            'X-Service-Key' => $serviceKey,
            'Authorization' => 'Bearer '.$jwt,
            'Accept' => 'application/json',
        ])->timeout(120)->post($url);

        if (! $resp->ok()) {
            return response()->json(
                ['error' => 'FastAPI audit returned HTTP '.$resp->status(), 'detail' => $resp->json()],
                502,
            );
        }

        return response()->json($resp->json());
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /**
     * Read the latest persisted completeness audit run for (workspace, pdf).
     *
     * Returns null when no run exists yet so the UI can render the empty
     * state with a "Run audit" CTA.
     *
     * @return array{
     *     finding_run_id: string,
     *     created_at: string,
     *     counts: array{error:int, warn:int, info:int},
     *     findings: list<array{
     *         finding_kind: string,
     *         severity: string,
     *         description: string,
     *         source_page: int|null,
     *         evidence: array<string,mixed>
     *     }>
     * }|null
     */
    private function loadLatestCompletenessAudit(string $workspaceId, string $pdfId): ?array
    {
        try {
            $latestRunId = DB::table('silver.completeness_findings')
                ->where('workspace_id', $workspaceId)
                ->where('pdf_id', $pdfId)
                ->orderByDesc('created_at')
                ->value('finding_run_id');

            if ($latestRunId === null) {
                return null;
            }

            $rows = DB::table('silver.completeness_findings')
                ->where('finding_run_id', $latestRunId)
                ->orderByRaw(
                    'CASE severity '
                    ."WHEN 'error' THEN 0 WHEN 'warn' THEN 1 WHEN 'info' THEN 2 ELSE 3 END",
                )
                ->orderByRaw('source_page NULLS LAST')
                ->orderBy('created_at')
                ->get();

            $createdAt = (string) DB::table('silver.completeness_findings')
                ->where('finding_run_id', $latestRunId)
                ->min('created_at');

            $counts = ['error' => 0, 'warn' => 0, 'info' => 0];
            $findings = [];
            foreach ($rows as $r) {
                $sev = (string) $r->severity;
                if (isset($counts[$sev])) {
                    $counts[$sev]++;
                }
                $findings[] = [
                    'finding_kind' => (string) $r->finding_kind,
                    'severity' => $sev,
                    'description' => (string) $r->description,
                    'source_page' => $r->source_page !== null ? (int) $r->source_page : null,
                    'evidence' => $this->decodeJsonb($r->evidence),
                ];
            }

            return [
                'finding_run_id' => (string) $latestRunId,
                'created_at' => $createdAt,
                'counts' => $counts,
                'findings' => $findings,
            ];
        } catch (\Throwable $e) {
            return null;
        }
    }

    /**
     * @return array{0: Project, 1: object, 2: string}
     */
    private function resolveContext(Request $request, string $slug, string $reportId): array
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        $workspaceId = (string) DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->value('workspace_id');
        $this->setWorkspaceRlsContext($workspaceId);

        $report = DB::table('silver.reports')
            ->where('report_id', $reportId)
            ->first();

        if ($report === null) {
            abort(404);
        }

        return [$project, $report, $workspaceId];
    }

    /**
     * Resolve the §04p bronze pdf_id (SHA-256 hex) for a silver.reports row.
     *
     * silver.reports.source_file_sha256 IS the §04p pdf_id — the same
     * SHA-256 hex used as the Bronze object key (added in migration
     * 2026_04_13_000000_add_report_versioning.php).
     */
    private function resolvePdfIdForReport(string $reportId): ?string
    {
        $sha = DB::table('silver.reports')
            ->where('report_id', $reportId)
            ->value('source_file_sha256');

        return $sha === null ? null : (string) $sha;
    }

    /** @return array<string, mixed> */
    private function decodeJsonb(mixed $raw): array
    {
        if (is_array($raw)) {
            return $raw;
        }
        if (is_string($raw)) {
            $decoded = json_decode($raw, true);

            return is_array($decoded) ? $decoded : [];
        }
        if (is_object($raw)) {
            return (array) $raw;
        }

        return [];
    }
}
