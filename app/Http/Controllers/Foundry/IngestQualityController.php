<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
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
 * Promotion gate: passes when acceptRate >= 95% AND fatalFiles == 0.
 */
class IngestQualityController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $anomalies = collect();

        $reports = DB::table('silver.reports AS r')
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
            ->get();

        $fileRows = $reports->map(function ($r) {
            $quality = $r->parse_quality_pct !== null ? (float) $r->parse_quality_pct : null;
            $status = match (true) {
                $quality === null => 'unassessed',
                $quality >= 0.7 => 'ok',
                $quality >= 0.3 => 'warn',
                default => 'regex_incomplete',
            };

            return [
                'file_id' => (string) $r->report_id,
                'name' => (string) ($r->title ?? 'Untitled report'),
                'format' => $r->is_scanned ? 'SCANNED PDF' : 'PDF',
                'size_bytes' => null,
                'rows' => (int) $r->passages,
                'accepted' => (int) $r->embedded,
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
