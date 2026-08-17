<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/IngestQualityController — post-import trust-moment surface.
 *
 * 2026-08-17 — this controller previously read exclusively from the §04p
 * dual-write OCR quality stack (silver.document_ingestion_quality,
 * silver.low_confidence_page_reviews, etc.). That stack has no writer on
 * the live path — P04P_DUAL_WRITE_ENABLED is false in production — so
 * this page always rendered its empty state for every real project,
 * indistinguishable from "nothing has been ingested yet" even when chat
 * was already answering questions from that project's documents.
 *
 * Rebuilt to read what the live ingest_pdf.py path actually writes:
 *   - silver.reports (one row per document: parser_used, is_scanned,
 *     parse_quality_pct) for the per-file list.
 *   - silver.document_passages, joined through silver.reports, for the
 *     rows/accepted counts (passage count / embedded-passage count).
 *   - silver.review_queue (the real, live OCR/low-confidence review
 *     routing table — see INSERT_OCR_REVIEW_SQL in ingest_pdf.py) for
 *     flagged/rejected/awaiting-OCR counts, instead of the never-written
 *     silver.low_confidence_page_reviews.
 *
 * review_queue rows aren't attributable to a specific report_id (the
 * table only carries project_id + bronze_uri), so flagged/rejected stay
 * project-level totals rather than a fabricated per-file breakdown —
 * the per-file table's flagged/rejected columns are left null (renders
 * as "—") rather than guessed.
 *
 * 2026-08-17 (follow-up) — two more bugs found live on this exact page:
 *
 * 1. silver.document_passages carries fail-closed RLS
 *    (document_passages_workspace_isolation — no NULL-GUC fallback,
 *    unlike silver.reports/review_queue which are still permissive). This
 *    controller never bound app.workspace_id, so the leftJoinSub above
 *    silently joined zero rows for every project — rows/accepted always
 *    rendered 0 even for projects with tens of thousands of real,
 *    embedded passages. Confirmed live: a project with 20,613 passages
 *    across document_passages showed 0/0 on every file row. Same class
 *    of bug IngestionRunsController::loadReports() already hit and fixed
 *    2026-08-15 — this controller was written after that fix and missed
 *    it. Wrapped the query in withWorkspaceRls() to match.
 *
 * 2. Per-file status was gated on parse_quality_pct (fraction of the 17
 *    baseline NI 43-101 numbered section headings found via regex — see
 *    pdf_report.py's NI43_BASELINE_SECTIONS). That's a structural-coverage
 *    metric, not an extraction-quality metric: a document that isn't
 *    shaped like an NI 43-101 technical report (a corporate presentation,
 *    an extracted drill-intercept table, a report using non-numbered or
 *    differently-formatted headings) will never contain 17 "N. Title"
 *    lines and will show "regex incomplete" even when every page parsed
 *    and embedded cleanly. On this "trust moment" page that reads as an
 *    ingestion failure when the ingestion actually succeeded. Status is
 *    now derived from what actually determines whether chat can use the
 *    document — passage count and embedded fraction — with structural
 *    coverage left out of the gate.
 *
 * Promotion gate: passes when acceptRate >= 95% AND fatalFiles == 0.
 */
class IngestQualityController extends Controller
{
    use SetsWorkspaceRlsContext;

    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $anomalies = collect();

        $reports = $this->withWorkspaceRls(
            (string) $project->workspace_id,
            fn () => DB::table('silver.reports AS r')
                ->leftJoinSub(
                    DB::table('silver.document_passages')
                        ->selectRaw('document_id, COUNT(*) AS passages, COUNT(*) FILTER (WHERE embedding_id IS NOT NULL) AS embedded')
                        ->groupBy('document_id'),
                    'p',
                    'p.document_id',
                    '=',
                    'r.report_id',
                )
                ->where('r.project_id', $project->project_id)
                ->orderByDesc('r.created_at')
                ->limit(50)
                ->select('r.report_id', 'r.title', 'r.is_scanned', 'r.parse_quality_pct', DB::raw('COALESCE(p.passages, 0) AS passages'), DB::raw('COALESCE(p.embedded, 0) AS embedded'))
                ->get(),
        );

        $fileRows = $reports->map(function ($r) {
            $passages = (int) $r->passages;
            $embedded = (int) $r->embedded;
            // Real signal for "did this document actually make it into
            // the retrievable index" — not parse_quality_pct (see class
            // docblock item 2). unassessed = nothing extracted yet;
            // error = extracted but nothing embedded; warn = partial.
            $status = match (true) {
                $passages === 0 => 'unassessed',
                $embedded === 0 => 'error',
                $embedded < $passages => 'warn',
                default => 'ok',
            };

            return [
                'file_id' => (string) $r->report_id,
                'name' => (string) ($r->title ?? 'Untitled report'),
                'format' => $r->is_scanned ? 'SCANNED PDF' : 'PDF',
                'size_bytes' => null,
                'rows' => $passages,
                'accepted' => $embedded,
                // review_queue (the real flag/reject source below) has no
                // report_id column, so per-file attribution isn't possible
                // with the live schema — left null rather than guessed.
                'flagged' => null,
                'rejected' => null,
                'status' => $status,
                'crs_detected' => null,
                'crs_confidence' => null,
                'duration_seconds' => null,
            ];
        })->values();

        // Project-level review_queue rollup — the real, live OCR/low-
        // confidence routing table (routing_decision/lifecycle/decision_kind
        // come from the review_routing_enum / review_lifecycle_enum /
        // review_decision_enum types — see 2026_05_24_120000_create_
        // silver_review_queue).
        $reviewRollup = DB::table('silver.review_queue')
            ->where('project_id', $project->project_id)
            ->selectRaw("
                COUNT(*) FILTER (WHERE lifecycle NOT IN ('committed', 'archived')) AS flagged,
                COUNT(*) FILTER (WHERE decision_kind = 'reject') AS rejected,
                COUNT(*) FILTER (WHERE routing_decision = 'review_required' AND lifecycle = 'pending') AS awaiting_ocr
            ")
            ->first();

        $passagesTotal = (int) $fileRows->sum('rows');
        $flagged = (int) ($reviewRollup->flagged ?? 0);
        $rejected = (int) ($reviewRollup->rejected ?? 0);
        $awaiting = (int) ($reviewRollup->awaiting_ocr ?? 0);
        // "Accepted" = content units that never entered the review queue at
        // all. flagged/rejected are review_queue rows (OCR-page granularity,
        // same order of magnitude as passages); clamped so a project with
        // more open review items than passages doesn't go negative.
        $accepted = max(0, $passagesTotal - $flagged - $rejected);

        $rowsTotal = $accepted + $flagged + $rejected;
        $passGate = $rowsTotal === 0 ? false : (($accepted / max(1, $rowsTotal)) >= 0.95 && $rejected === 0);

        return Inertia::render('Foundry/IngestQuality', [
            'import_id' => (string) ($request->query('import') ?? 'latest'),
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'files' => $fileRows,
            'anomalies' => $anomalies,
            'totals' => [
                'accepted' => $accepted,
                'flagged' => $flagged,
                'rejected' => $rejected,
                'awaiting_ocr' => $awaiting,
            ],
            'pass_gate' => $passGate,
            'empty' => $fileRows->isEmpty(),
        ]);
    }
}
